#!/usr/bin/env python3
"""Compute Layer 3 mart_cd_market_metric rows from Layer 2 enriched facts.

Usage:
  python pipeline/scripts/etl/layer3_compute_cd.py --cd-market cd_017 --dry-run
  python pipeline/scripts/etl/layer3_compute_cd.py --cd-market cd_017
  python pipeline/scripts/etl/layer3_compute_cd.py --all

Phase 16-G-2 executes dry-run only. Non-dry-run INSERT support is included for
Phase 16-G-3, but this phase must not execute it.
"""

from __future__ import annotations

import argparse
import json
import math
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

from layer3_compute import (  # noqa: E402
    JW_TARGET_BRAND_IDS,
    clean_text,
    growth_warning_flags,
    load_env,
    simple_markdown_table,
    source_metric_sql,
)
from layer3_compute_extended import (  # noqa: E402
    ANOMALY_THRESHOLDS,
    EXTENDED_METRIC_COLUMNS,
    compute_extended_metrics,
    period_kind_and_ord,
    safe_db_float,
)
from layer3_normalize import (  # noqa: E402
    is_monthly_period,
    period_sort_key,
    prev_month,
    prev_quarter_month,
    safe_div,
    same_month_prev_year,
    validate_periods,
)
from ops_utils import configure_logging, find_project_root, first_existing, retry  # noqa: E402


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
OUTPUT_DIR = PROJECT_ROOT / "output"
CATALOG_DIR = OUTPUT_DIR / "catalog"
ENRICHED_DIR = OUTPUT_DIR / "enriched"
AUDIT_DIR = PROJECT_ROOT / "audits" / "phase_16g2_cd_market_etl"
KST = ZoneInfo("Asia/Seoul")
SOURCES = ("ubist", "nsa", "chso", "csd")
COMPUTATION_VERSION = "v1"
DRY_RUN_PREFIX = Path("/tmp")
CD_OUTPUT_COLUMNS = [
    "cd_market_id",
    "cd_brand_id",
    "cd_brand_name",
    "ml_id",
    "is_jw",
    "period_yyyymm",
    "channel",
    "specialty",
    "channel_norm",
    "specialty_norm",
    "market_share",
    "mom",
    "qoq",
    "yoy",
    "mat",
    "growth_abs",
    "rank_in_market",
    "raw_value",
    "raw_count",
    *EXTENDED_METRIC_COLUMNS,
    "payload",
    "computed_at",
    "computation_version",
    "extended_metric_version",
]


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


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


def is_jw_target_brand_id(brand_id: Any) -> bool:
    return clean_text(brand_id) in JW_TARGET_BRAND_IDS


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    paths = {
        "cd_market": CATALOG_DIR / "cd_market" / "cd_market.parquet",
        "cd_brand": CATALOG_DIR / "cd_brand" / "cd_brand.parquet",
        "cd_product": CATALOG_DIR / "cd_product" / "cd_product.parquet",
    }
    missing = [name for name, path in paths.items() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing cd catalog parquet(s): {', '.join(missing)}")
    return (
        pd.read_parquet(paths["cd_market"]),
        pd.read_parquet(paths["cd_brand"]),
        pd.read_parquet(paths["cd_product"]),
    )


def cd_market_ids_from_catalog() -> list[str]:
    cd_market, _, _ = load_catalogs()
    return sorted(cd_market["cd_id"].dropna().unique().tolist())


def build_cd_bridge(cd_market_id: str, cd_brand: pd.DataFrame, cd_product: pd.DataFrame) -> pd.DataFrame:
    """Return product-to-cd-brand rows for one cd_market."""
    brand_rows = cd_brand[cd_brand["cd_id"] == cd_market_id].copy()
    product_rows = cd_product[cd_product["cd_id"] == cd_market_id].copy()
    if brand_rows.empty:
        raise ValueError(f"{cd_market_id}: no cd_brand rows")
    if product_rows.empty:
        raise ValueError(f"{cd_market_id}: no cd_product rows")

    brand_rows["cd_brand_name"] = brand_rows["name"].fillna(brand_rows["merge_name"]).fillna("")
    brand_rows["is_jw"] = brand_rows["brand_id"].map(is_jw_target_brand_id)
    bridge = product_rows[["product_id", "brand_id", "ml_id"]].merge(
        brand_rows[["brand_id", "cd_brand_name", "is_jw"]],
        on="brand_id",
        how="inner",
    )
    bridge = bridge.rename(columns={"brand_id": "cd_brand_id"})
    return bridge[["product_id", "cd_brand_id", "cd_brand_name", "ml_id", "is_jw"]].drop_duplicates()


def cd_market_definition(cd_market_id: str, cd_market: pd.DataFrame) -> dict[str, Any]:
    row = cd_market[cd_market["cd_id"] == cd_market_id]
    if row.empty:
        raise ValueError(f"{cd_market_id}: no cd_market row")
    return row.iloc[0].to_dict()


def enriched_path_for_ml(ml_id: str) -> Path:
    path = ENRICHED_DIR / f"ml_id={ml_id}" / "data.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing enriched parquet for {ml_id}: {path}")
    return path


def aggregate_level_cd(
    con: duckdb.DuckDBPyConnection,
    cd_market_id: str,
    ml_id: str,
    level: str,
    enriched_path: Path,
) -> pd.DataFrame:
    if level == "total":
        channel_expr = "CAST(NULL AS VARCHAR) AS channel"
        specialty_expr = "CAST(NULL AS VARCHAR) AS specialty"
        where_extra = ""
    elif level == "channel":
        channel_expr = "e.channel AS channel"
        specialty_expr = "CAST(NULL AS VARCHAR) AS specialty"
        where_extra = "AND e.channel IS NOT NULL"
    elif level == "channel_specialty":
        channel_expr = "e.channel AS channel"
        specialty_expr = "e.specialty AS specialty"
        where_extra = "AND e.channel IS NOT NULL AND e.specialty IS NOT NULL"
    else:
        raise ValueError(f"unsupported aggregation level: {level}")

    source_exprs = [expr for source in SOURCES for expr in source_metric_sql(source)]
    source_columns = ",\n            ".join(source_exprs)

    sql = f"""
        WITH base AS (
          SELECT
            b.cd_brand_id,
            b.cd_brand_name,
            b.is_jw,
            e.period_yyyymm,
            {channel_expr},
            {specialty_expr},
            e.product_id,
            COALESCE(e.canonical_value, 0) AS canonical_value,
            COALESCE(e.source, 'unknown') AS source
          FROM read_parquet('{enriched_path.as_posix()}') e
          INNER JOIN cd_bridge b ON e.product_id = b.product_id
          WHERE 1 = 1
            {where_extra}
        ),
        raw_agg AS (
          SELECT
            cd_brand_id,
            cd_brand_name,
            is_jw,
            period_yyyymm,
            channel,
            specialty,
            SUM(canonical_value) AS raw_value,
            COUNT(*) AS raw_count,
            COUNT(DISTINCT product_id) AS product_count,
            {source_columns}
          FROM base
          GROUP BY cd_brand_id, cd_brand_name, is_jw, period_yyyymm, channel, specialty
        ),
        ranked AS (
          SELECT
            *,
            SUM(raw_value) OVER (PARTITION BY period_yyyymm, channel, specialty) AS market_raw,
            DENSE_RANK() OVER (PARTITION BY period_yyyymm, channel, specialty ORDER BY raw_value DESC) AS rank_in_market
          FROM raw_agg
          WHERE raw_count > 0
        )
        SELECT
          '{cd_market_id}' AS cd_market_id,
          '{ml_id}' AS ml_id,
          cd_brand_id,
          cd_brand_name,
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
    LOGGER.info("[%s] Aggregating level=%s", cd_market_id, level)
    df = con.execute(sql).df()
    LOGGER.info("[%s] level=%s rows=%s", cd_market_id, level, f"{len(df):,}")
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


def compute_growth_metrics_cd(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    keys_no_period = ["cd_brand_id", "channel", "specialty"]
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


def compute_mat_cd(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["mat"] = None
    monthly_mask = result["period_yyyymm"].map(is_monthly_period)
    if not monthly_mask.any():
        return result

    monthly = result.loc[monthly_mask].copy()
    monthly["period_ord"] = monthly["period_yyyymm"].map(period_sort_key)
    monthly = monthly.sort_values(["cd_brand_id", "channel", "specialty", "period_ord"])

    mat_values = pd.Series(index=monthly.index, dtype="float64")
    for _, part in monthly.groupby(["cd_brand_id", "channel", "specialty"], dropna=False, sort=False):
        rolling_sum = part["raw_value"].rolling(window=12, min_periods=12).sum()
        period_span = part["period_ord"] - part["period_ord"].shift(11)
        mat_values.loc[part.index] = rolling_sum.where(period_span == 11)

    result.loc[mat_values.index, "mat"] = mat_values
    return result


def build_payloads_cd(df: pd.DataFrame) -> list[str]:
    payloads: list[str] = []
    payload_cols = [
        "cd_market_id",
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
    threshold_warning_cols = [
        col
        for col in ("growth_contribution_warning", "ei_warning")
        if col in df.columns
    ]
    for row in df[payload_cols + threshold_warning_cols].itertuples(index=False):
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
            "cd_market_id": row.cd_market_id,
            "source_split": source_split,
            "source_count": source_count,
            "product_count": int(row.product_count or 0),
            "aggregation_level": row.aggregation_level,
        }
        warnings = growth_warning_flags(row.mom, row.qoq, row.yoy)
        for col in threshold_warning_cols:
            flag = getattr(row, col)
            if flag is not None and not pd.isna(flag):
                warnings.append(str(flag))
        if warnings:
            payload["warnings"] = warnings
        payloads.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return payloads


def finalize_cd_metrics(df: pd.DataFrame) -> pd.DataFrame:
    validate_periods(df["period_yyyymm"].dropna().unique())
    result = compute_growth_metrics_cd(df)
    result = compute_mat_cd(result)
    result["channel_norm"] = result["channel"].fillna("__ALL__")
    result["specialty_norm"] = result["specialty"].fillna("__ALL__")
    period_meta = [period_kind_and_ord(period) for period in result["period_yyyymm"]]
    result["period_kind"] = [item[0] for item in period_meta]
    result["period_ord"] = [item[1] for item in period_meta]

    result["brand_id"] = result["cd_brand_id"]
    result = compute_extended_metrics(result)
    result = result.drop(columns=["brand_id"])
    result["payload"] = build_payloads_cd(result)
    result["computed_at"] = now_kst()
    result["computation_version"] = COMPUTATION_VERSION

    numeric_cols = [
        "market_share",
        "mom",
        "qoq",
        "yoy",
        "mat",
        "growth_abs",
        "raw_value",
        *EXTENDED_METRIC_COLUMNS,
    ]
    for col in numeric_cols:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    result["raw_count"] = pd.to_numeric(result["raw_count"], errors="coerce").fillna(0).astype("int64")
    result["rank_in_market"] = pd.to_numeric(result["rank_in_market"], errors="coerce").fillna(0).astype("int64")
    result["is_jw"] = result["is_jw"].fillna(False).astype(bool)
    return result[CD_OUTPUT_COLUMNS].sort_values(
        ["cd_market_id", "cd_brand_id", "period_yyyymm", "channel", "specialty"],
        na_position="first",
    ).reset_index(drop=True)


def compute_cd_market(cd_market_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    cd_market, cd_brand, cd_product = load_catalogs()
    cd_def = cd_market_definition(cd_market_id, cd_market)
    ml_id = str(cd_def["ml_id"])
    enriched_path = enriched_path_for_ml(ml_id)
    bridge = build_cd_bridge(cd_market_id, cd_brand, cd_product)

    con = duckdb.connect()
    con.register("cd_bridge", bridge)
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
          COUNT(DISTINCT b.cd_brand_id) AS mapped_brands
        FROM read_parquet('{enriched_path.as_posix()}') e
        INNER JOIN cd_bridge b ON e.product_id = b.product_id
        """
    ).fetchone()
    level_frames = [
        aggregate_level_cd(con, cd_market_id, ml_id, level, enriched_path)
        for level in ("total", "channel", "channel_specialty")
    ]
    con.close()

    result = finalize_cd_metrics(pd.concat(level_frames, ignore_index=True))
    stats = {
        "cd_market_id": cd_market_id,
        "cd_market_name": cd_def.get("name"),
        "ml_id": ml_id,
        "data_source": cd_def.get("data_source"),
        "catalog_cd_brands": int(bridge["cd_brand_id"].nunique()),
        "catalog_cd_products": int(bridge["product_id"].nunique()),
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


def validate_cd_metrics(df: pd.DataFrame) -> dict[str, Any]:
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
    result["raw_value_nan_rows"] = int(work["raw_value"].isna().sum())
    result["raw_value_negative_rows"] = int((work["raw_value"] < 0).fillna(False).sum())
    result["hhi_negative_rows"] = int((pd.to_numeric(work["hhi"], errors="coerce") < 0).fillna(False).sum())

    anomaly = anomaly_distribution_cd(work)
    result["extended_anomaly_rows_any"] = int(anomaly["rows"].sum())
    result["extended_anomaly_max_pct"] = 0.0 if anomaly.empty else float(anomaly["pct"].max())
    result["error_count"] = int(
        result["ms_sum_bad_groups"]
        + result["ms_gt_one_rows"]
        + result["ms_negative_rows"]
        + result["rank_bad_groups"]
        + result["raw_value_nan_rows"]
        + result["raw_value_negative_rows"]
        + result["hhi_negative_rows"]
    )
    result["hard_anomaly"] = result["error_count"] > 0
    return result


def anomaly_distribution_cd(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total = len(df)
    for name, (column, op, threshold) in ANOMALY_THRESHOLDS.items():
        series = pd.to_numeric(df[column], errors="coerce")
        if op == "abs_gt":
            mask = series.abs() > threshold
        elif op == "gt":
            mask = series > threshold
        else:
            raise ValueError(f"unsupported anomaly op: {op}")
        count = int(mask.fillna(False).sum())
        rows.append(
            {
                "anomaly": name,
                "metric": column,
                "threshold": threshold,
                "rows": count,
                "pct": 0.0 if total == 0 else count / total * 100,
            }
        )
    return pd.DataFrame(rows)


def metric_distribution(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for metric in EXTENDED_METRIC_COLUMNS:
        series = pd.to_numeric(df[metric], errors="coerce")
        rows.append(
            {
                "metric": metric,
                "count": int(series.count()),
                "fill_pct": 0.0 if len(df) == 0 else float(series.count() / len(df) * 100),
                "null_pct": 0.0 if len(df) == 0 else float(series.isna().mean() * 100),
                "mean": None if series.count() == 0 else float(series.mean()),
                "std": None if series.count() == 0 else float(series.std()),
                "min": None if series.count() == 0 else float(series.min()),
                "max": None if series.count() == 0 else float(series.max()),
            }
        )
    return pd.DataFrame(rows)


def estimate_cd_load_rows(sample_cd_market_id: str, sample_rows: int) -> dict[str, Any]:
    _, cd_brand, _ = load_catalogs()
    brand_counts = cd_brand.groupby("cd_id")["brand_id"].nunique().to_dict()
    sample_brands = int(brand_counts.get(sample_cd_market_id, 0))
    total_brands = int(sum(brand_counts.values()))
    if not sample_brands:
        return {"sample_brands": 0, "total_brands": total_brands, "estimated_rows": None}
    return {
        "sample_brands": sample_brands,
        "total_brands": total_brands,
        "brand_ratio": total_brands / sample_brands,
        "estimated_rows": int(round(sample_rows * (total_brands / sample_brands))),
    }


def write_dry_run_artifacts(
    df: pd.DataFrame,
    stats: dict[str, Any],
    validation: dict[str, Any],
    cd_market_id: str,
    dry_run_log: str | None = None,
) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_csv = DRY_RUN_PREFIX / f"cd_dry_run_{cd_market_id}.csv"
    df.to_csv(tmp_csv, index=False)

    sample_csv = AUDIT_DIR / "06_dry_run_sample.csv"
    df.head(100).to_csv(sample_csv, index=False)
    distribution = metric_distribution(df)
    distribution.to_csv(AUDIT_DIR / "04_metric_distribution.csv", index=False)
    anomaly = anomaly_distribution_cd(df)
    anomaly.to_csv(AUDIT_DIR / "05_anomaly_distribution.csv", index=False)

    breakdown = level_breakdown(df)
    estimate = estimate_cd_load_rows(cd_market_id, len(df))
    mapped_pct = 0 if stats["catalog_cd_products"] == 0 else stats["mapped_products"] / stats["catalog_cd_products"] * 100

    mapping_inventory = [
        "# 01. Mapping Inventory",
        "",
        f"- cd_market: `{cd_market_id}` / {stats['cd_market_name']}",
        f"- parent ml_id: `{stats['ml_id']}`",
        f"- data_source: `{stats['data_source']}`",
        f"- cd_brand rows: {stats['catalog_cd_brands']:,}",
        f"- cd_product rows: {stats['catalog_cd_products']:,}",
        f"- Layer 2 mapped products: {stats['mapped_products']:,} ({mapped_pct:.2f}%)",
        "",
        "Mapping path: `cd_market.cd_id` -> `cd_brand.cd_id` -> `cd_product.product_id` -> Layer 2 enriched facts.",
    ]
    (AUDIT_DIR / "01_mapping_inventory.md").write_text("\n".join(mapping_inventory) + "\n", encoding="utf-8")

    data_flow = [
        "# 02. Data Flow",
        "",
        "1. Load `cd_market`, `cd_brand`, and `cd_product` from `output/catalog`.",
        "2. Resolve one `cd_market_id` to its parent `ml_id`.",
        "3. Read only the matching Layer 2 enriched partition.",
        "4. Join enriched rows to `cd_product.product_id`.",
        "5. Aggregate three levels: total, channel, channel x specialty.",
        "6. Compute 7 base metrics and 8 extended metrics in memory.",
        "7. Write dry-run CSV/artifacts only; no `mart_cd_market_metric` INSERT in 16-G-2.",
        "",
        f"Layer 2 input partition rows: {stats['layer2_rows']:,}",
        f"Mapped Layer 2 rows: {stats['mapped_rows']:,}",
    ]
    (AUDIT_DIR / "02_data_flow.md").write_text("\n".join(data_flow) + "\n", encoding="utf-8")

    dry_run_results = [
        f"# 03. Dry-run Results ({cd_market_id})",
        "",
        "## Row counts",
        f"- Layer 2 input partition: {stats['layer2_rows']:,}",
        f"- Layer 2 mapped rows: {stats['mapped_rows']:,}",
        f"- Layer 3 output: {len(df):,}",
        f"  - total: {int(breakdown.get('total', 0)):,}",
        f"  - channel: {int(breakdown.get('channel', 0)):,}",
        f"  - channel x specialty: {int(breakdown.get('channel_specialty', 0)):,}",
        "",
        "## Integrity",
        f"- MS groups checked: {validation['ms_groups_checked']:,}",
        f"- MS bad groups: {validation['ms_sum_bad_groups']:,}",
        f"- rank bad groups: {validation['rank_bad_groups']:,}",
        f"- raw_value NaN rows: {validation['raw_value_nan_rows']:,}",
        f"- ERROR count: {validation['error_count']:,}",
        f"- max extended anomaly pct: {validation['extended_anomaly_max_pct']:.2f}%",
    ]
    (AUDIT_DIR / "03_dry_run_results.md").write_text("\n".join(dry_run_results) + "\n", encoding="utf-8")

    anomaly_md = [
        "# 05. Anomaly Check",
        "",
        simple_markdown_table(anomaly),
        "",
        f"Gate: max anomaly pct must be below 5%. Observed max: {validation['extended_anomaly_max_pct']:.2f}%.",
    ]
    (AUDIT_DIR / "05_anomaly_check.md").write_text("\n".join(anomaly_md) + "\n", encoding="utf-8")

    estimate_lines = [
        "# 07. Estimated Load",
        "",
        f"- sample cd_market: `{cd_market_id}`",
        f"- sample rows: {len(df):,}",
        f"- sample cd_brand count: {estimate['sample_brands']:,}",
        f"- total cd_brand count: {estimate['total_brands']:,}",
        f"- brand-ratio estimate: {estimate.get('brand_ratio', 0):.4f}",
        f"- estimated 19 cd_market rows: {estimate['estimated_rows']:,}" if estimate.get("estimated_rows") is not None else "- estimated 19 cd_market rows: n/a",
        "",
        "This is a brand-count proportional estimate. Actual row count depends on per-market source period and channel/specialty coverage.",
    ]
    (AUDIT_DIR / "07_estimated_load.md").write_text("\n".join(estimate_lines) + "\n", encoding="utf-8")

    if dry_run_log is not None:
        (AUDIT_DIR / "08_apply_log.txt").write_text(dry_run_log, encoding="utf-8")

    summary = [
        "# Phase 16-G-2 Summary",
        "",
        f"Dry-run completed for `{cd_market_id}` without hard metric anomalies.",
        "",
        "| item | value |",
        "|---|---|",
        f"| parent ml_id | {stats['ml_id']} |",
        f"| Layer 2 mapped rows | {stats['mapped_rows']:,} |",
        f"| Layer 3 output | {len(df):,} |",
        f"| total level | {int(breakdown.get('total', 0)):,} |",
        f"| channel level | {int(breakdown.get('channel', 0)):,} |",
        f"| channel_specialty level | {int(breakdown.get('channel_specialty', 0)):,} |",
        f"| ERROR | {validation['error_count']:,} |",
        f"| max anomaly pct | {validation['extended_anomaly_max_pct']:.2f}% |",
        f"| estimated 19 cd_market rows | {estimate['estimated_rows']:,}" if estimate.get("estimated_rows") is not None else "| estimated 19 cd_market rows | n/a |",
        "",
        "Next: Phase 16-G-3 executes the actual `mart_cd_market_metric` INSERT.",
    ]
    (AUDIT_DIR / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return tmp_csv


def insert_to_mariadb_cd(df: pd.DataFrame, cd_market_id: str, batch_size: int = 5000) -> int:
    sql = """
        INSERT INTO mart_cd_market_metric
          (cd_market_id, cd_brand_id, cd_brand_name, ml_id, is_jw,
           period_yyyymm, channel, specialty,
           market_share, mom, qoq, yoy, mat, growth_abs, rank_in_market,
           raw_value, raw_count,
           cagr_1y, cagr_3y, cagr_5y, ei_5y, momentum_score, growth_contribution, hhi, market_cagr_5y,
           payload, computation_version, extended_metric_version)
        VALUES
          (%(cd_market_id)s, %(cd_brand_id)s, %(cd_brand_name)s, %(ml_id)s, %(is_jw)s,
           %(period_yyyymm)s, %(channel)s, %(specialty)s,
           %(market_share)s, %(mom)s, %(qoq)s, %(yoy)s, %(mat)s, %(growth_abs)s, %(rank_in_market)s,
           %(raw_value)s, %(raw_count)s,
           %(cagr_1y)s, %(cagr_3y)s, %(cagr_5y)s, %(ei_5y)s, %(momentum_score)s, %(growth_contribution)s, %(hhi)s, %(market_cagr_5y)s,
           %(payload)s, %(computation_version)s, %(extended_metric_version)s)
        ON DUPLICATE KEY UPDATE
          cd_brand_name = VALUES(cd_brand_name),
          ml_id = VALUES(ml_id),
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
          cagr_1y = VALUES(cagr_1y),
          cagr_3y = VALUES(cagr_3y),
          cagr_5y = VALUES(cagr_5y),
          ei_5y = VALUES(ei_5y),
          momentum_score = VALUES(momentum_score),
          growth_contribution = VALUES(growth_contribution),
          hhi = VALUES(hhi),
          market_cagr_5y = VALUES(market_cagr_5y),
          payload = VALUES(payload),
          computation_version = VALUES(computation_version),
          extended_metric_version = VALUES(extended_metric_version),
          computed_at = CURRENT_TIMESTAMP
    """
    work = df.copy()
    for col in EXTENDED_METRIC_COLUMNS + ["market_share", "mom", "qoq", "yoy", "mat", "growth_abs", "raw_value"]:
        work[col] = work[col].map(safe_db_float)
    records = work.astype(object).where(pd.notna(work), None).to_dict("records")
    inserted = 0
    with mariadb_connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("DELETE FROM mart_cd_market_metric WHERE cd_market_id = %s", (cd_market_id,))
                for start in range(0, len(records), batch_size):
                    batch = records[start : start + batch_size]
                    cur.executemany(sql, batch)
                    conn.commit()
                    inserted += len(batch)
                    LOGGER.info("[%s] inserted/upserted %s rows", cd_market_id, f"{inserted:,}")
            except Exception:
                conn.rollback()
                raise
    return inserted


def dry_run(cd_market_id: str) -> int:
    df, stats = compute_cd_market(cd_market_id)
    validation = validate_cd_metrics(df)
    csv_path = write_dry_run_artifacts(df, stats, validation, cd_market_id)
    print(f"\n=== {cd_market_id} dry-run ===")
    print(df.head(20).to_string(index=False))
    print(f"\nLayer 2 mapped rows: {stats['mapped_rows']:,}")
    print(f"Layer 3 output: {len(df):,}")
    print("Level breakdown:")
    print(level_breakdown(df).to_string())
    print("\nMetric fill rate:")
    dist = metric_distribution(df)
    for row in dist.itertuples(index=False):
        print(f"  {row.metric}: {row.fill_pct:.2f}% filled")
    anomaly = anomaly_distribution_cd(df)
    print("\nAnomaly check:")
    for row in anomaly.itertuples(index=False):
        print(f"  {row.anomaly}: {row.rows:,} rows ({row.pct:.2f}%)")
    print(f"\nDry-run CSV: {csv_path}")
    print(f"ERROR: {validation['error_count']:,}")
    print(f"Max anomaly pct: {validation['extended_anomaly_max_pct']:.2f}%")
    return 1 if validation["hard_anomaly"] or validation["extended_anomaly_max_pct"] > 5 else 0


def run_load(cd_market_ids: Iterable[str]) -> None:
    for cd_market_id in cd_market_ids:
        df, _ = compute_cd_market(cd_market_id)
        validation = validate_cd_metrics(df)
        if validation["hard_anomaly"] or validation["extended_anomaly_max_pct"] > 5:
            raise RuntimeError(f"[{cd_market_id}] anomaly before insert: {validation}")
        inserted = insert_to_mariadb_cd(df, cd_market_id)
        LOGGER.info("[%s] load complete: %s rows", cd_market_id, f"{inserted:,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cd-market", help="One cd_market id to compute, e.g. cd_017")
    parser.add_argument("--all", action="store_true", help="Compute all cd markets")
    parser.add_argument("--dry-run", action="store_true", help="Write dry-run artifacts only; do not insert")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.cd_market:
        raise SystemExit("--all and --cd-market are mutually exclusive")
    if not args.all and not args.cd_market:
        raise SystemExit("Provide --cd-market CD_ID or --all")

    cd_market_ids = cd_market_ids_from_catalog() if args.all else [args.cd_market]
    if args.dry_run:
        if args.all:
            raise SystemExit("--dry-run currently supports one --cd-market at a time for PL review")
        return dry_run(args.cd_market)
    run_load(cd_market_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
