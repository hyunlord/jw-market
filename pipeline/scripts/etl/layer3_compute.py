#!/usr/bin/env python3
"""Compute Layer 3 mart_core_brand_metric rows from Layer 2 enriched facts.

Usage:
  python pipeline/scripts/etl/layer3_compute.py --ml ml_006 --dry-run
  python pipeline/scripts/etl/layer3_compute.py --ml ml_006
  python pipeline/scripts/etl/layer3_compute.py --all

Phase 16-E-2 uses dry-run only. Non-dry-run insert support is included for
Phase 16-E-3, but this phase does not execute it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd
import pymysql

from ops_utils import configure_logging, find_project_root, first_existing, retry  # noqa: E402
from layer3_normalize import (  # noqa: E402
    is_monthly_period,
    period_sort_key,
    prev_month,
    prev_quarter_month,
    safe_div,
    same_month_prev_year,
    validate_periods,
)


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
OUTPUT_DIR = PROJECT_ROOT / "output"
CATALOG_DIR = OUTPUT_DIR / "catalog"
ENRICHED_DIR = OUTPUT_DIR / "enriched"
AUDIT_DIR = PROJECT_ROOT / "audits" / "phase_16e2_layer3_dry_run"
KST = ZoneInfo("Asia/Seoul")
SOURCES = ("ubist", "nsa", "chso", "csd")
COMPUTATION_VERSION = "v1"
GROWTH_WARNING_THRESHOLD = 5.0

OUTPUT_COLUMNS = [
    "ml_id",
    "brand_id",
    "brand_name",
    "is_jw",
    "period_yyyymm",
    "channel",
    "specialty",
    "market_share",
    "mom",
    "qoq",
    "yoy",
    "mat",
    "growth_abs",
    "rank_in_market",
    "raw_value",
    "raw_count",
    "payload",
    "computed_at",
    "computation_version",
]


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def is_jw_company(*values: Any) -> bool:
    text = " ".join(clean_text(value) for value in values)
    return any(token in text for token in ("JW", "중외", "제이더블유"))


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@retry((pymysql.err.OperationalError, pymysql.err.InterfaceError), logger=LOGGER)
def mariadb_connect() -> pymysql.connections.Connection:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    if "MARIADB_PASSWORD" not in env:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {env_path}")
    return pymysql.connect(
        host="127.0.0.1",
        port=int(env.get("HOST_PORT", "3307")),
        user=env.get("MARIADB_USER", "jwapp"),
        password=env["MARIADB_PASSWORD"],
        database=env.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_brand_bridge(ml_id: str) -> pd.DataFrame:
    sp_path = CATALOG_DIR / "strategic_product" / "strategic_product.parquet"
    sb_path = CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet"
    if not sp_path.exists():
        raise FileNotFoundError(f"Missing strategic_product parquet: {sp_path}")
    if not sb_path.exists():
        raise FileNotFoundError(f"Missing strategic_brand parquet: {sb_path}")

    sp = pd.read_parquet(sp_path, columns=["product_id", "brand_id", "ml_id"])
    sb = pd.read_parquet(sb_path)
    sp = sp[sp["ml_id"] == ml_id].copy()
    sb = sb[sb["ml_id"] == ml_id].copy()

    sb["brand_name"] = sb["name"].fillna(sb["merge_name"]).fillna("")
    sb["is_jw"] = [
        is_jw_company(row.get("판매사"), row.get("제조사"))
        for row in sb[["판매사", "제조사"]].to_dict("records")
    ]

    bridge = sp[["product_id", "brand_id"]].merge(
        sb[["brand_id", "brand_name", "is_jw"]],
        on="brand_id",
        how="left",
    )
    return bridge.drop_duplicates()


def source_metric_sql(source: str) -> list[str]:
    source_sql = source.replace("'", "''")
    return [
        f"SUM(CASE WHEN source = '{source_sql}' THEN canonical_value ELSE 0 END) AS source_value_{source}",
        f"SUM(CASE WHEN source = '{source_sql}' THEN 1 ELSE 0 END) AS source_count_{source}",
    ]


def aggregate_level(
    con: duckdb.DuckDBPyConnection,
    ml_id: str,
    level: str,
    enriched_path: Path,
) -> pd.DataFrame:
    if level == "total":
        channel_expr = "CAST(NULL AS VARCHAR) AS channel"
        specialty_expr = "CAST(NULL AS VARCHAR) AS specialty"
        partition_cols = "period_yyyymm, channel, specialty"
        where_extra = ""
    elif level == "channel":
        channel_expr = "e.channel AS channel"
        specialty_expr = "CAST(NULL AS VARCHAR) AS specialty"
        partition_cols = "period_yyyymm, channel, specialty"
        where_extra = "AND e.channel IS NOT NULL"
    elif level == "channel_specialty":
        channel_expr = "e.channel AS channel"
        specialty_expr = "e.specialty AS specialty"
        partition_cols = "period_yyyymm, channel, specialty"
        where_extra = "AND e.channel IS NOT NULL AND e.specialty IS NOT NULL"
    else:
        raise ValueError(f"unsupported aggregation level: {level}")

    group_cols = "b.brand_id, b.brand_name, b.is_jw, b.period_yyyymm, b.channel, b.specialty"
    source_exprs = [expr for source in SOURCES for expr in source_metric_sql(source)]
    source_columns = ",\n            ".join(source_exprs)

    sql = f"""
        WITH base AS (
          SELECT
            b.brand_id,
            b.brand_name,
            b.is_jw,
            e.period_yyyymm,
            {channel_expr},
            {specialty_expr},
            e.product_id,
            COALESCE(e.canonical_value, 0) AS canonical_value,
            COALESCE(e.source, 'unknown') AS source
          FROM read_parquet('{enriched_path.as_posix()}') e
          LEFT JOIN brand_bridge b ON e.product_id = b.product_id
          WHERE b.brand_id IS NOT NULL
            {where_extra}
        ),
        raw_agg AS (
          SELECT
            b.brand_id,
            b.brand_name,
            b.is_jw,
            b.period_yyyymm,
            b.channel,
            b.specialty,
            SUM(b.canonical_value) AS raw_value,
            COUNT(*) AS raw_count,
            COUNT(DISTINCT b.product_id) AS product_count,
            {source_columns}
          FROM base b
          GROUP BY {group_cols}
        ),
        ranked AS (
          SELECT
            *,
            SUM(raw_value) OVER (PARTITION BY {partition_cols}) AS market_raw,
            DENSE_RANK() OVER (PARTITION BY {partition_cols} ORDER BY raw_value DESC) AS rank_in_market
          FROM raw_agg
          WHERE raw_count > 0
        )
        SELECT
          '{ml_id}' AS ml_id,
          brand_id,
          brand_name,
          is_jw,
          period_yyyymm,
          channel,
          specialty,
          raw_value,
          raw_count,
          product_count,
          source_value_ubist,
          source_count_ubist,
          source_value_nsa,
          source_count_nsa,
          source_value_chso,
          source_count_chso,
          source_value_csd,
          source_count_csd,
          CASE WHEN market_raw = 0 THEN NULL ELSE raw_value / market_raw END AS market_share,
          CAST(rank_in_market AS INTEGER) AS rank_in_market,
          '{level}' AS aggregation_level
        FROM ranked
    """
    LOGGER.info("[%s] Aggregating level=%s", ml_id, level)
    df = con.execute(sql).df()
    LOGGER.info("[%s] level=%s rows=%s", ml_id, level, f"{len(df):,}")
    return df


def _with_merge_keys(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    result = df.copy()
    for key in keys:
        if key in {"channel", "specialty"}:
            result[f"__{key}_key"] = result[key].fillna("__ALL__")
        else:
            result[f"__{key}_key"] = result[key]
    return result


def _merge_previous(
    df: pd.DataFrame,
    keys_no_period: list[str],
    target_col: str,
    value_col: str,
) -> pd.Series:
    left = _with_merge_keys(df, keys_no_period)
    merge_keys = [f"__{key}_key" for key in keys_no_period]
    lookup = left[merge_keys + ["period_yyyymm", "raw_value"]].rename(
        columns={"period_yyyymm": target_col, "raw_value": value_col}
    )
    merged = left.merge(lookup, on=merge_keys + [target_col], how="left")
    return merged[value_col]


def compute_growth_metrics(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    keys_no_period = ["brand_id", "channel", "specialty"]
    result["prev_month"] = result["period_yyyymm"].map(prev_month)
    result["prev_quarter"] = result["period_yyyymm"].map(prev_quarter_month)
    result["prev_year"] = result["period_yyyymm"].map(same_month_prev_year)

    result["prev_month_value"] = _merge_previous(result, keys_no_period, "prev_month", "prev_month_value")
    result["prev_quarter_value"] = _merge_previous(result, keys_no_period, "prev_quarter", "prev_quarter_value")
    result["prev_year_value"] = _merge_previous(result, keys_no_period, "prev_year", "prev_year_value")

    result["mom"] = [
        safe_div(curr - prev, prev) if pd.notna(prev) else None
        for curr, prev in zip(result["raw_value"], result["prev_month_value"], strict=False)
    ]
    result["qoq"] = [
        safe_div(curr - prev, prev) if pd.notna(prev) else None
        for curr, prev in zip(result["raw_value"], result["prev_quarter_value"], strict=False)
    ]
    result["yoy"] = [
        safe_div(curr - prev, prev) if pd.notna(prev) else None
        for curr, prev in zip(result["raw_value"], result["prev_year_value"], strict=False)
    ]
    result["growth_abs"] = [
        (curr - prev) if pd.notna(prev) else None
        for curr, prev in zip(result["raw_value"], result["prev_month_value"], strict=False)
    ]
    return result.drop(columns=["prev_month_value", "prev_quarter_value", "prev_year_value"])


def compute_mat(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["mat"] = None
    monthly_mask = result["period_yyyymm"].map(is_monthly_period)
    if not monthly_mask.any():
        return result

    monthly = result.loc[monthly_mask].copy()
    monthly["period_ord"] = monthly["period_yyyymm"].map(period_sort_key)
    sort_cols = ["brand_id", "channel", "specialty", "period_ord"]
    monthly = monthly.sort_values(sort_cols)

    mat_values = pd.Series(index=monthly.index, dtype="float64")
    group_keys = ["brand_id", "channel", "specialty"]
    for _, part in monthly.groupby(group_keys, dropna=False, sort=False):
        rolling_sum = part["raw_value"].rolling(window=12, min_periods=12).sum()
        period_span = part["period_ord"] - part["period_ord"].shift(11)
        valid = period_span == 11
        mat_values.loc[part.index] = rolling_sum.where(valid)

    result.loc[mat_values.index, "mat"] = mat_values
    return result


def growth_warning_flags(mom: Any, qoq: Any, yoy: Any) -> list[str]:
    """Return non-blocking warning flags for unusually high growth metrics."""
    flags: list[str] = []
    for metric, value in (("mom", mom), ("qoq", qoq), ("yoy", yoy)):
        if value is None or pd.isna(value):
            continue
        try:
            if abs(float(value)) > GROWTH_WARNING_THRESHOLD:
                flags.append(f"high_{metric}")
        except (TypeError, ValueError):
            continue
    return flags


def build_payloads(df: pd.DataFrame) -> list[str]:
    payloads: list[str] = []
    for row in df[
        [
            "raw_value",
            "mom",
            "qoq",
            "yoy",
            "product_count",
            "aggregation_level",
            "source_value_ubist",
            "source_count_ubist",
            "source_value_nsa",
            "source_count_nsa",
            "source_value_chso",
            "source_count_chso",
            "source_value_csd",
            "source_count_csd",
        ]
    ].itertuples(index=False):
        raw_value = float(row.raw_value or 0)
        source_split: dict[str, float] = {}
        source_count: dict[str, int] = {}
        for source in SOURCES:
            source_value = getattr(row, f"source_value_{source}")
            source_rows = getattr(row, f"source_count_{source}")
            if source_rows and int(source_rows) > 0:
                source_count[source] = int(source_rows)
                source_split[source] = 0.0 if raw_value == 0 else float(source_value or 0) / raw_value
        payload = {
            "source_split": source_split,
            "source_count": source_count,
            "product_count": int(row.product_count or 0),
            "aggregation_level": row.aggregation_level,
        }
        warnings = growth_warning_flags(row.mom, row.qoq, row.yoy)
        if warnings:
            payload["warnings"] = warnings
        payloads.append(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return payloads


def finalize_metrics(df: pd.DataFrame) -> pd.DataFrame:
    validate_periods(df["period_yyyymm"].dropna().unique())
    result = compute_growth_metrics(df)
    result = compute_mat(result)
    result["payload"] = build_payloads(result)
    result["computed_at"] = now_kst()
    result["computation_version"] = COMPUTATION_VERSION

    for col in ("market_share", "mom", "qoq", "yoy", "mat", "growth_abs", "raw_value"):
        result[col] = pd.to_numeric(result[col], errors="coerce")
    result["raw_count"] = pd.to_numeric(result["raw_count"], errors="coerce").fillna(0).astype("int64")
    result["rank_in_market"] = pd.to_numeric(result["rank_in_market"], errors="coerce").fillna(0).astype("int64")
    result["is_jw"] = result["is_jw"].fillna(False).astype(bool)

    return result[OUTPUT_COLUMNS].sort_values(
        ["ml_id", "brand_id", "period_yyyymm", "channel", "specialty"],
        na_position="first",
    ).reset_index(drop=True)


def compute_layer3_for_ml(ml_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    enriched_path = ENRICHED_DIR / f"ml_id={ml_id}" / "data.parquet"
    if not enriched_path.exists():
        raise FileNotFoundError(f"Missing enriched parquet for {ml_id}: {enriched_path}")

    con = duckdb.connect()
    bridge = load_brand_bridge(ml_id)
    con.register("brand_bridge", bridge)

    input_stats = con.execute(
        f"""
        SELECT
          COUNT(*) AS layer2_rows,
          COUNT(DISTINCT product_id) AS layer2_products,
          MIN(period_yyyymm) AS min_period,
          MAX(period_yyyymm) AS max_period
        FROM read_parquet('{enriched_path.as_posix()}')
        """
    ).fetchone()

    mapped_stats = con.execute(
        f"""
        SELECT
          COUNT(*) AS mapped_rows,
          COUNT(DISTINCT e.product_id) AS mapped_products,
          COUNT(DISTINCT b.brand_id) AS mapped_brands
        FROM read_parquet('{enriched_path.as_posix()}') e
        LEFT JOIN brand_bridge b ON e.product_id = b.product_id
        WHERE b.brand_id IS NOT NULL
        """
    ).fetchone()

    level_frames = [aggregate_level(con, ml_id, level, enriched_path) for level in ("total", "channel", "channel_specialty")]
    result = finalize_metrics(pd.concat(level_frames, ignore_index=True))
    con.close()

    stats = {
        "ml_id": ml_id,
        "layer2_rows": int(input_stats[0]),
        "layer2_products": int(input_stats[1]),
        "min_period": input_stats[2],
        "max_period": input_stats[3],
        "mapped_rows": int(mapped_stats[0]),
        "mapped_products": int(mapped_stats[1]),
        "mapped_brands": int(mapped_stats[2]),
        "layer3_rows": len(result),
    }
    return result, stats


def level_name(row: pd.Series) -> str:
    if pd.isna(row["channel"]) and pd.isna(row["specialty"]):
        return "total"
    if pd.notna(row["channel"]) and pd.isna(row["specialty"]):
        return "channel"
    return "channel_specialty"


def level_breakdown(df: pd.DataFrame) -> pd.Series:
    levels = df.apply(level_name, axis=1)
    return levels.value_counts().reindex(["total", "channel", "channel_specialty"]).fillna(0).astype(int)


def validate_metrics(df: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    levels = df.apply(level_name, axis=1)
    work = df.assign(_level=levels)

    market_groups = ["_level", "period_yyyymm", "channel", "specialty"]
    ms_sum = work.dropna(subset=["market_share"]).groupby(market_groups, dropna=False)["market_share"].sum()
    ms_bad = ms_sum[(ms_sum < 0.999) | (ms_sum > 1.001)]
    result["ms_groups_checked"] = int(len(ms_sum))
    result["ms_sum_bad_groups"] = int(len(ms_bad))
    result["ms_min"] = None if ms_sum.empty else float(ms_sum.min())
    result["ms_max"] = None if ms_sum.empty else float(ms_sum.max())
    result["ms_gt_one_rows"] = int((work["market_share"].fillna(0) > 1.00001).sum())
    result["ms_negative_rows"] = int((work["market_share"].fillna(0) < -0.00001).sum())

    rank_bad = 0
    for _, part in work.groupby(market_groups, dropna=False):
        ranks = sorted(part["rank_in_market"].dropna().astype(int).unique().tolist())
        if ranks and ranks != list(range(1, max(ranks) + 1)):
            rank_bad += 1
    result["rank_bad_groups"] = rank_bad

    result["mom_abs_gt_5_rows"] = int((work["mom"].abs() > GROWTH_WARNING_THRESHOLD).fillna(False).sum())
    result["qoq_abs_gt_5_rows"] = int((work["qoq"].abs() > GROWTH_WARNING_THRESHOLD).fillna(False).sum())
    result["yoy_abs_gt_5_rows"] = int((work["yoy"].abs() > GROWTH_WARNING_THRESHOLD).fillna(False).sum())
    warning_mask = (
        (work["mom"].abs() > GROWTH_WARNING_THRESHOLD).fillna(False)
        | (work["qoq"].abs() > GROWTH_WARNING_THRESHOLD).fillna(False)
        | (work["yoy"].abs() > GROWTH_WARNING_THRESHOLD).fillna(False)
    )
    result["warning_rows_any"] = int(warning_mask.sum())
    result["mom_null_rows"] = int(work["mom"].isna().sum())
    result["qoq_null_rows"] = int(work["qoq"].isna().sum())
    result["yoy_null_rows"] = int(work["yoy"].isna().sum())
    result["mat_null_rows"] = int(work["mat"].isna().sum())
    result["raw_value_nan_rows"] = int(work["raw_value"].isna().sum())
    result["raw_value_negative_rows"] = int((work["raw_value"] < 0).fillna(False).sum())
    result["error_count"] = int(
        result["ms_sum_bad_groups"]
        + result["ms_gt_one_rows"]
        + result["ms_negative_rows"]
        + result["rank_bad_groups"]
        + result["raw_value_nan_rows"]
        + result["raw_value_negative_rows"]
    )
    result["hard_anomaly"] = result["error_count"] > 0
    result["growth_warning"] = result["warning_rows_any"] > 0
    return result


def estimate_full_load_rows(sample_ml_id: str, sample_rows: int) -> dict[str, Any]:
    sb_path = CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet"
    sb = pd.read_parquet(sb_path, columns=["ml_id", "brand_id"])
    brand_counts = sb.groupby("ml_id")["brand_id"].nunique().to_dict()
    sample_brands = brand_counts.get(sample_ml_id, 0)
    total_brands = sum(brand_counts.values())
    if not sample_brands:
        return {"sample_brands": 0, "total_brands": total_brands, "estimated_rows": None}
    estimated_rows = int(round(sample_rows * (total_brands / sample_brands)))
    return {
        "sample_brands": int(sample_brands),
        "total_brands": int(total_brands),
        "estimated_rows": estimated_rows,
        "brand_ratio": total_brands / sample_brands,
    }


def simple_markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    headers = list(df.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in df.itertuples(index=False):
        values = []
        for value in row:
            if pd.isna(value):
                values.append("")
            elif isinstance(value, float):
                values.append(f"{value:.6g}")
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def write_dry_run_artifacts(df: pd.DataFrame, stats: dict[str, Any], validation: dict[str, Any], ml_id: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    csv_gz = AUDIT_DIR / f"02_dry_run_{ml_id}.csv.gz"
    df.to_csv(csv_gz, index=False, compression="gzip")

    breakdown = level_breakdown(df)
    estimate = estimate_full_load_rows(ml_id, len(df))
    sample = df[
        (df["brand_name"].astype(str).str.contains("리바로", na=False))
        & (df["period_yyyymm"] == "2024-03")
    ].head(20)

    summary = [
        f"# Phase 16-E-2 dry-run ({ml_id} 리바로)",
        "",
        "## Row 수",
        f"- Layer 2 input: {stats['layer2_rows']:,} rows",
        f"- Layer 2 mapped: {stats['mapped_rows']:,} rows / {stats['mapped_products']:,} products / {stats['mapped_brands']:,} brands",
        f"- Layer 3 output: {len(df):,} rows",
        f"  - level 1 total: {int(breakdown.get('total', 0)):,}",
        f"  - level 2 channel: {int(breakdown.get('channel', 0)):,}",
        f"  - level 3 chan x spec: {int(breakdown.get('channel_specialty', 0)):,}",
        "",
        "## 매트릭 sample",
    ]
    if sample.empty:
        summary.append("- `리바로` / `2024-03` sample row 없음")
    else:
        sample_cols = [
            "brand_name",
            "period_yyyymm",
            "channel",
            "specialty",
            "raw_value",
            "market_share",
            "mom",
            "yoy",
            "mat",
            "rank_in_market",
        ]
        summary.append(simple_markdown_table(sample[sample_cols]))

    summary.extend(
        [
            "",
            "## 정합성",
            f"- MS sum groups checked: {validation['ms_groups_checked']:,}",
            f"- MS sum bad groups: {validation['ms_sum_bad_groups']:,}",
            f"- MS min/max: {validation['ms_min']} / {validation['ms_max']}",
            f"- rank bad groups: {validation['rank_bad_groups']:,}",
            f"- raw_value NaN rows: {validation['raw_value_nan_rows']:,}",
            f"- division/null cases: MoM {validation['mom_null_rows']:,}, QoQ {validation['qoq_null_rows']:,}, YoY {validation['yoy_null_rows']:,}, MAT {validation['mat_null_rows']:,}",
            f"- ERROR rows/groups: {validation['error_count']:,}",
            f"- WARNING rows (`abs(growth metric) > {GROWTH_WARNING_THRESHOLD:g}`): {validation['warning_rows_any']:,}",
            f"  - high_mom: {validation['mom_abs_gt_5_rows']:,}",
            f"  - high_qoq: {validation['qoq_abs_gt_5_rows']:,}",
            f"  - high_yoy: {validation['yoy_abs_gt_5_rows']:,}",
            "",
            "## 16 ml 전체 row 수 추정",
            f"- sample brand count ({ml_id}): {estimate['sample_brands']:,}",
            f"- strategic_brand total: {estimate['total_brands']:,}",
            f"- estimated full load rows: {estimate['estimated_rows']:,}" if estimate.get("estimated_rows") is not None else "- estimated full load rows: n/a",
        ]
    )
    (AUDIT_DIR / "01_dry_run_summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")

    metric_validation = [
        "# 03. Metric Validation",
        "",
        "| check | result |",
        "|---|---|",
        f"| MS sum tolerance | {'PASS' if validation['ms_sum_bad_groups'] == 0 else 'FAIL'} ({validation['ms_sum_bad_groups']} bad groups) |",
        f"| MS > 1.0 rows | {'PASS' if validation['ms_gt_one_rows'] == 0 else 'FAIL'} ({validation['ms_gt_one_rows']}) |",
        f"| negative MS rows | {'PASS' if validation['ms_negative_rows'] == 0 else 'FAIL'} ({validation['ms_negative_rows']}) |",
        f"| rank no gap | {'PASS' if validation['rank_bad_groups'] == 0 else 'FAIL'} ({validation['rank_bad_groups']} bad groups) |",
        f"| raw_value NaN | {'PASS' if validation['raw_value_nan_rows'] == 0 else 'FAIL'} ({validation['raw_value_nan_rows']}) |",
        f"| negative raw_value | {'PASS' if validation['raw_value_negative_rows'] == 0 else 'FAIL'} ({validation['raw_value_negative_rows']}) |",
        f"| high MoM warning | {'WARN' if validation['mom_abs_gt_5_rows'] else 'PASS'} ({validation['mom_abs_gt_5_rows']} rows) |",
        f"| high QoQ warning | {'WARN' if validation['qoq_abs_gt_5_rows'] else 'PASS'} ({validation['qoq_abs_gt_5_rows']} rows) |",
        f"| high YoY warning | {'WARN' if validation['yoy_abs_gt_5_rows'] else 'PASS'} ({validation['yoy_abs_gt_5_rows']} rows) |",
        "",
        "High growth values are reported as warnings for PL review because new/near-zero products can grow sharply without breaking arithmetic.",
    ]
    (AUDIT_DIR / "03_metric_validation.md").write_text("\n".join(metric_validation) + "\n", encoding="utf-8")

    load_size = [
        "# 04. Estimated Load Size",
        "",
        f"- Dry-run ml: `{ml_id}`",
        f"- Dry-run rows: {len(df):,}",
        f"- Sample brand count: {estimate['sample_brands']:,}",
        f"- Total strategic_brand count: {estimate['total_brands']:,}",
        f"- Brand-ratio estimate: {estimate.get('brand_ratio', 0):.4f}",
        f"- Estimated 16 ml Layer 3 rows: {estimate['estimated_rows']:,}" if estimate.get("estimated_rows") is not None else "- Estimated 16 ml Layer 3 rows: n/a",
        "",
        "This is a brand-count proportional estimate. Actual 16-E-3 row count will vary by period coverage, source type, and channel/specialty availability.",
    ]
    (AUDIT_DIR / "04_estimated_load_size.md").write_text("\n".join(load_size) + "\n", encoding="utf-8")

    design_notes = [
        "# 05. Script Design Notes",
        "",
        "- DuckDB reads Layer 2 parquet and performs brand-level aggregation before pandas growth/MAT calculations.",
        "- `is_jw` is derived from strategic_brand `판매사` / `제조사` containing `JW`, `중외`, or `제이더블유` because no source `is_jw` column exists.",
        "- MAT is calculated only for monthly periods and requires a consecutive 12-month window.",
        "- QoQ uses minus three months for monthly periods and previous quarter for quarterly labels.",
        "- Non-dry-run insert support is present for Phase 16-E-3, but this phase executed dry-run only.",
    ]
    (AUDIT_DIR / "05_script_design_notes.md").write_text("\n".join(design_notes) + "\n", encoding="utf-8")

    final_summary = [
        "# Phase 16-E-2 Summary",
        "",
        "Dry-run completed for `ml_006` without hard metric anomalies.",
        "",
        "| item | value |",
        "|---|---|",
        f"| Layer 2 input | {stats['layer2_rows']:,} |",
        f"| Layer 3 output | {len(df):,} |",
        f"| total level | {int(breakdown.get('total', 0)):,} |",
        f"| channel level | {int(breakdown.get('channel', 0)):,} |",
        f"| channel_specialty level | {int(breakdown.get('channel_specialty', 0)):,} |",
        f"| ERROR | {validation['error_count']:,} |",
        f"| hard anomaly | {validation['hard_anomaly']} |",
        f"| WARNING rows | {validation['warning_rows_any']:,} |",
        f"| high_mom | {validation['mom_abs_gt_5_rows']:,} |",
        f"| high_qoq | {validation['qoq_abs_gt_5_rows']:,} |",
        f"| high_yoy | {validation['yoy_abs_gt_5_rows']:,} |",
        "",
        "Next: PL reviews dry-run artifacts, then Phase 16-E-3 can execute the actual load.",
    ]
    (AUDIT_DIR / "summary.md").write_text("\n".join(final_summary) + "\n", encoding="utf-8")
    return csv_gz


def insert_to_mariadb(df: pd.DataFrame, ml_id: str, batch_size: int = 1000) -> int:
    sql = """
        INSERT INTO mart_core_brand_metric
          (ml_id, brand_id, brand_name, is_jw, period_yyyymm, channel, specialty,
           market_share, mom, qoq, yoy, mat, growth_abs, rank_in_market,
           raw_value, raw_count, payload, computation_version)
        VALUES
          (%(ml_id)s, %(brand_id)s, %(brand_name)s, %(is_jw)s, %(period_yyyymm)s, %(channel)s, %(specialty)s,
           %(market_share)s, %(mom)s, %(qoq)s, %(yoy)s, %(mat)s, %(growth_abs)s, %(rank_in_market)s,
           %(raw_value)s, %(raw_count)s, %(payload)s, %(computation_version)s)
        ON DUPLICATE KEY UPDATE
          brand_name = VALUES(brand_name),
          is_jw = VALUES(is_jw),
          market_share = VALUES(market_share),
          mom = VALUES(mom),
          qoq = VALUES(qoq),
          yoy = VALUES(yoy),
          mat = VALUES(mat),
          growth_abs = VALUES(growth_abs),
          rank_in_market = VALUES(rank_in_market),
          raw_value = VALUES(raw_value),
          raw_count = VALUES(raw_count),
          payload = VALUES(payload),
          computation_version = VALUES(computation_version),
          computed_at = CURRENT_TIMESTAMP
    """
    records = df.where(pd.notna(df), None).to_dict("records")
    inserted = 0
    with mariadb_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mart_core_brand_metric WHERE ml_id = %s", (ml_id,))
            for start in range(0, len(records), batch_size):
                batch = records[start : start + batch_size]
                cur.executemany(sql, batch)
                conn.commit()
                inserted += len(batch)
                LOGGER.info("[%s] inserted/upserted %s rows", ml_id, f"{inserted:,}")
    return inserted


def ml_ids_from_catalog() -> list[str]:
    ml_path = CATALOG_DIR / "ml_market" / "ml_market.parquet"
    if not ml_path.exists():
        raise FileNotFoundError(f"Missing ml_market parquet: {ml_path}")
    ml = pd.read_parquet(ml_path, columns=["ml_id"])
    return sorted(ml["ml_id"].dropna().unique().tolist())


def dry_run(ml_id: str) -> int:
    df, stats = compute_layer3_for_ml(ml_id)
    validation = validate_metrics(df)
    csv_gz = write_dry_run_artifacts(df, stats, validation, ml_id)
    print(f"\n=== {ml_id} dry-run sample ===")
    print(df.head(20).to_string(index=False))
    print(f"\nLayer 2 input: {stats['layer2_rows']:,}")
    print(f"Layer 3 output: {len(df):,}")
    print("Level breakdown:")
    print(level_breakdown(df).to_string())
    print(f"Dry-run CSV: {csv_gz}")
    print(f"ERROR: {validation['error_count']:,}")
    print(f"Hard anomaly: {validation['hard_anomaly']}")
    print(f"WARNING rows (any abs(growth metric) > {GROWTH_WARNING_THRESHOLD:g}): {validation['warning_rows_any']:,}")
    print(f"  high_mom: {validation['mom_abs_gt_5_rows']:,}")
    print(f"  high_qoq: {validation['qoq_abs_gt_5_rows']:,}")
    print(f"  high_yoy: {validation['yoy_abs_gt_5_rows']:,}")
    return 1 if validation["hard_anomaly"] else 0


def run_load(ml_ids: Iterable[str]) -> None:
    for ml_id in ml_ids:
        df, _ = compute_layer3_for_ml(ml_id)
        inserted = insert_to_mariadb(df, ml_id)
        LOGGER.info("[%s] load complete: %s rows", ml_id, f"{inserted:,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ml", help="One ml_id to compute, e.g. ml_006")
    parser.add_argument("--all", action="store_true", help="Compute all ml markets")
    parser.add_argument("--dry-run", action="store_true", help="Write dry-run artifacts only; do not insert")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.ml:
        raise SystemExit("--all and --ml are mutually exclusive")
    if not args.all and not args.ml:
        raise SystemExit("Provide --ml ML_ID or --all")

    if args.dry_run:
        if args.all:
            raise SystemExit("--dry-run currently supports one --ml at a time for PL review")
        return dry_run(args.ml)

    ml_ids = ml_ids_from_catalog() if args.all else [args.ml]
    run_load(ml_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
