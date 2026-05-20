#!/usr/bin/env python3
"""Build source-aware JSON Layer 3 general-view rows.

Phase 16-G-4-Fix-ETL-v3 writes dry-run artifacts only. Real INSERT into the
six mart tables is intentionally deferred to Phase 16-G-4-Fix-Load.

Usage:
  python pipeline/scripts/etl/layer3_compute_general_v3.py --source ubist --dry-run --limit-atc4 5
  python pipeline/scripts/etl/layer3_compute_general_v3.py --source iqvia_nsa --dry-run --limit-atc4 5
  python pipeline/scripts/etl/layer3_compute_general_v3.py --all --dry-run --limit-atc4 5
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd
import pymysql

from brand_key_normalize import best_name, normalize_brand_name
from layer3_compute_extended import (
    compute_cagr_value,
    compute_ei,
    compute_growth_contribution,
    compute_hhi,
    compute_momentum,
)
from layer3_normalize import (
    period_range_mat,
    period_sort_key,
    prev_month,
    prev_quarter_month,
    safe_div,
    same_month_prev_year,
)
from ops_utils import configure_logging, find_project_root, first_existing, retry


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
OUTPUT_DIR = PROJECT_ROOT / "output"
CATALOG_DIR = OUTPUT_DIR / "catalog"
ENRICHED_GLOB = str(OUTPUT_DIR / "enriched" / "ml_id=*" / "data.parquet")
UBIST_GLOB = str(OUTPUT_DIR / "ubist" / "year=*" / "month=*" / "data.parquet")
DRY_RUN_DIR = Path("/tmp")
ALLOWED_SOURCES = ("ubist", "iqvia_nsa")
LAYER2_SOURCE = {"ubist": "ubist", "iqvia_nsa": "nsa"}
MEASURES_BY_SOURCE = {
    "ubist": ("sales", "volume"),
    "iqvia_nsa": ("sales", "unit", "dosage_unit", "counting_unit"),
}
MEASURE_COLUMN_MAP = {
    ("ubist", "sales"): "raw_rx_amt",
    ("ubist", "volume"): "raw_rx_qty",
    ("iqvia_nsa", "sales"): "raw_rx_amt",
    ("iqvia_nsa", "unit"): "raw_units",
    ("iqvia_nsa", "dosage_unit"): "raw_dosage_units",
    ("iqvia_nsa", "counting_unit"): "raw_counting_units",
}
UNIT_LABELS = {
    ("ubist", "sales"): "KRW",
    ("ubist", "volume"): "Rx",
    ("iqvia_nsa", "sales"): "KRW",
    ("iqvia_nsa", "unit"): "unit",
    ("iqvia_nsa", "dosage_unit"): "dosage unit",
    ("iqvia_nsa", "counting_unit"): "counting unit",
}
GENERAL_BRAND_JSONL = DRY_RUN_DIR / "general_v3_{source}_brand_rows.jsonl"
GENERAL_MARKET_JSONL = DRY_RUN_DIR / "general_v3_{source}_market_rows.jsonl"


def general_brand_jsonl_path(source: str, ml: str | None = None) -> Path:
    suffix = f"_{ml}" if ml else ""
    return DRY_RUN_DIR / f"general_v3_{source}{suffix}_brand_rows.jsonl"


def general_market_jsonl_path(source: str, ml: str | None = None) -> Path:
    suffix = f"_{ml}" if ml else ""
    return DRY_RUN_DIR / f"general_v3_{source}{suffix}_market_rows.jsonl"


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
def mariadb_connect(cursorclass=pymysql.cursors.DictCursor) -> pymysql.connections.Connection:
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
        autocommit=True,
        cursorclass=cursorclass,
    )


def safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def dumps(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False, separators=(",", ":"))


def extract_atc4(value: Any) -> tuple[str, str | None]:
    if value is None or pd.isna(value):
        return "", None
    text = str(value).strip()
    if not text:
        return "", None
    match = re.search(r"\[?([A-Z][0-9A-Z]{2,5})\]?", text.upper())
    code = match.group(1) if match else text.split("_", 1)[0].split()[0].strip("[]").upper()
    return code, text


def parse_ubist_drug_code(source_row_id: Any) -> str:
    parts = str(source_row_id or "").split("::")
    return parts[-1].strip() if len(parts) >= 6 else ""


def parse_nsa_row_id(source_row_id: Any) -> int | None:
    parts = str(source_row_id or "").split("::")
    if len(parts) == 2 and parts[0] == "nsa":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def load_product_catalog() -> pd.DataFrame:
    product_path = CATALOG_DIR / "strategic_product" / "strategic_product.parquet"
    df = pd.read_parquet(product_path)
    df = df.rename(columns={"name": "product_name", "merge_name": "brand_name"})
    keep = [
        "product_id",
        "product_name",
        "brand_name",
        "brand_id",
        "class",
        "molecule",
        "dosage_form",
        "strength_pack",
        "nhi_type",
        "ox_gx",
        "fish_oil",
        "판매사",
        "제조사",
    ]
    return df[[c for c in keep if c in df.columns]].drop_duplicates("product_id")


def load_layer2_sample(source: str, max_rows: int, ml: str | None = None) -> pd.DataFrame:
    layer2_source = LAYER2_SOURCE[source]
    where = [f"source = '{layer2_source}'"]
    if ml:
        where.append(f"ml_id = '{ml}'")
    sql = f"""
        SELECT
          ml_id,
          product_id,
          source,
          period_yyyymm,
          raw_rx_amt,
          raw_rx_cnt,
          raw_rx_qty,
          canonical_value,
          channel,
          specialty,
          source_table,
          source_row_id
        FROM read_parquet('{ENRICHED_GLOB}')
        WHERE {' AND '.join(where)}
        LIMIT {int(max_rows)}
    """
    con = duckdb.connect()
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def add_ubist_atc4(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["ubist_drug_code"] = result["source_row_id"].map(parse_ubist_drug_code)
    codes = [c for c in result["ubist_drug_code"].dropna().unique().tolist() if c]
    if not codes:
        result["atc4_code"] = "UNKNOWN"
        result["atc4_desc"] = None
        return result

    con = duckdb.connect()
    con.register("drug_codes", pd.DataFrame({"drug_code": codes}))
    try:
        mapping = con.execute(
            f"""
            SELECT DISTINCT
              cast(u.약품코드 AS varchar) AS drug_code,
              first(u.ATC) AS atc_text
            FROM read_parquet('{UBIST_GLOB}') AS u
            JOIN drug_codes AS c
              ON cast(u.약품코드 AS varchar) = c.drug_code
            GROUP BY 1
            """
        ).df()
    finally:
        con.close()

    atc_map = {row["drug_code"]: extract_atc4(row["atc_text"]) for _, row in mapping.iterrows()}
    atc_pairs = result["ubist_drug_code"].map(lambda code: atc_map.get(code, ("UNKNOWN", None)))
    result["atc4_code"] = atc_pairs.map(lambda pair: pair[0])
    result["atc4_desc"] = atc_pairs.map(lambda pair: pair[1])
    return result


def add_nsa_atc4_and_measures(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["nsa_raw_id"] = result["source_row_id"].map(parse_nsa_row_id)
    ids = [int(v) for v in result["nsa_raw_id"].dropna().unique().tolist()]
    result["raw_units"] = result["raw_rx_qty"]
    result["raw_dosage_units"] = None
    result["raw_counting_units"] = result["raw_rx_cnt"]
    if not ids:
        result["atc4_code"] = "UNKNOWN"
        result["atc4_desc"] = None
        return result

    rows: list[dict[str, Any]] = []
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            chunk_size = 1000
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start : start + chunk_size]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT
                      id,
                      JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static.ATC 4 CODE')) AS atc4,
                      JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static.ATC 4')) AS atc4_alt,
                      JSON_UNQUOTE(JSON_EXTRACT(payload, '$.period_values.Units')) AS units,
                      JSON_UNQUOTE(JSON_EXTRACT(payload, '$.period_values.Dosage Units')) AS dosage_units,
                      JSON_UNQUOTE(JSON_EXTRACT(payload, '$.period_values.Counting Units')) AS counting_units
                    FROM iqvia_nsa_quarterly_raw
                    WHERE id IN ({placeholders})
                    """,
                    chunk,
                )
                rows.extend(cur.fetchall())
    finally:
        conn.close()

    raw = pd.DataFrame(rows)
    if raw.empty:
        result["atc4_code"] = "UNKNOWN"
        result["atc4_desc"] = None
        return result
    raw["atc_tuple"] = raw.apply(lambda r: extract_atc4(r.get("atc4") or r.get("atc4_alt")), axis=1)
    raw["atc4_code"] = raw["atc_tuple"].map(lambda v: v[0] or "UNKNOWN")
    raw["atc4_desc"] = raw["atc_tuple"].map(lambda v: v[1])
    for col in ("units", "dosage_units", "counting_units"):
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw.rename(columns={"id": "nsa_raw_id"})
    result = result.merge(
        raw[["nsa_raw_id", "atc4_code", "atc4_desc", "units", "dosage_units", "counting_units"]],
        on="nsa_raw_id",
        how="left",
    )
    result["raw_units"] = result["units"].combine_first(pd.to_numeric(result["raw_units"], errors="coerce"))
    result["raw_dosage_units"] = result["dosage_units"]
    result["raw_counting_units"] = result["counting_units"].combine_first(pd.to_numeric(result["raw_counting_units"], errors="coerce"))
    result["atc4_code"] = result["atc4_code"].fillna("UNKNOWN")
    return result


def add_atc4(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if source == "ubist":
        return add_ubist_atc4(df)
    if source == "iqvia_nsa":
        return add_nsa_atc4_and_measures(df)
    raise ValueError(source)


def prepare_general_frame(source: str, max_rows: int, ml: str | None = None) -> pd.DataFrame:
    df = load_layer2_sample(source, max_rows=max_rows, ml=ml)
    if df.empty:
        return df
    products = load_product_catalog()
    df = df.merge(products, on="product_id", how="left")
    df["brand_name"] = df.apply(lambda r: best_name(r.get("brand_name"), r.get("product_name"), r.get("product_id")), axis=1)
    df["brand_key"] = df["brand_name"].map(normalize_brand_name)
    df = add_atc4(df, source)
    df["atc4_code"] = df["atc4_code"].replace("", "UNKNOWN").fillna("UNKNOWN")
    return df


def period_value_map(group: pd.DataFrame, value_col: str) -> dict[str, float]:
    series = group.groupby("period_yyyymm", dropna=False)[value_col].sum(min_count=1)
    return {str(k): float(v) for k, v in series.dropna().items()}


def value_at(history: dict[str, float], period: str | None) -> float | None:
    if not period:
        return None
    return history.get(period)


def pct_growth(current: float | None, previous: float | None) -> float | None:
    ratio = safe_div(current, previous)
    if ratio is None:
        return None
    return (ratio - 1.0) * 100


def mat_growth(history: dict[str, float], period: str) -> float | None:
    window = period_range_mat(period)
    if not window:
        return None
    previous_end = same_month_prev_year(period)
    previous_window = period_range_mat(previous_end) if previous_end else []
    if not previous_window:
        return None
    current = sum(history.get(p, 0.0) for p in window)
    previous = sum(history.get(p, 0.0) for p in previous_window)
    return pct_growth(current, previous)


def cagr_from_history(history: dict[str, float], period: str, years: int) -> float | None:
    try:
        ord_now = period_sort_key(period)
    except Exception:
        return None
    periods_per_year = 12 if "-Q" not in period else 4
    target_ord = ord_now - periods_per_year * years
    start_period = next((p for p in history if period_sort_key(p) == target_ord), None)
    return compute_cagr_value(history.get(period), history.get(start_period) if start_period else None, years)


def rank_maps(agg: pd.DataFrame, value_col: str) -> dict[tuple[str, str], int]:
    ranks: dict[tuple[str, str], int] = {}
    totals = agg.groupby(["atc4_code", "period_yyyymm", "brand_key"], dropna=False)[value_col].sum().reset_index()
    for (atc4_code, period), part in totals.groupby(["atc4_code", "period_yyyymm"], dropna=False):
        part = part.sort_values(value_col, ascending=False).reset_index(drop=True)
        for idx, row in part.iterrows():
            ranks[(str(row["brand_key"]), str(period))] = int(idx + 1)
    return ranks


def hhi_for_period(market_part: pd.DataFrame, value_col: str) -> float | None:
    total = market_part[value_col].sum()
    if not total:
        return None
    brand_totals = market_part.groupby("brand_key")[value_col].sum()
    shares = [(v / total) for v in brand_totals if v and not pd.isna(v)]
    return compute_hhi(shares)


def build_channel_or_specialty_data(group: pd.DataFrame, value_col: str, dim_col: str) -> dict[str, dict[str, dict[str, float]]]:
    data: dict[str, dict[str, dict[str, float]]] = {}
    if dim_col not in group.columns:
        return data
    for dim, part in group.groupby(dim_col, dropna=False):
        label = str(dim).strip() if dim is not None and not pd.isna(dim) and str(dim).strip() else "__ALL__"
        history = period_value_map(part, value_col)
        data[label] = {period: {"raw_value": value} for period, value in sorted(history.items(), key=lambda kv: period_sort_key(kv[0]))}
    return data


def build_brand_rows(source: str, measure: str, df: pd.DataFrame, value_col: str) -> list[dict[str, Any]]:
    working = df.loc[df[value_col].notna() & (df[value_col] > 0)].copy()
    if working.empty:
        return []

    market_period_totals = working.groupby(["atc4_code", "period_yyyymm"], dropna=False)[value_col].sum().to_dict()
    brand_period_totals = working.groupby(["brand_key", "atc4_code", "period_yyyymm"], dropna=False)[value_col].sum().reset_index()
    ranks = rank_maps(working, value_col)
    market_history_by_atc = {
        atc: period_value_map(part, value_col)
        for atc, part in working.groupby("atc4_code", dropna=False)
    }
    hhi_by_atc_period = {
        (str(atc), str(period)): hhi_for_period(part, value_col)
        for (atc, period), part in working.groupby(["atc4_code", "period_yyyymm"], dropna=False)
    }

    rows: list[dict[str, Any]] = []
    group_cols = ["brand_key", "atc4_code", "brand_id"]
    for (brand_key, atc4_code, brand_id), group in working.groupby(group_cols, dropna=False):
        history = period_value_map(group, value_col)
        atc_history = market_history_by_atc.get(atc4_code, {})
        metric_history: dict[str, dict[str, Any]] = {}
        extended_history: dict[str, dict[str, Any]] = {}
        sorted_periods = sorted(history, key=period_sort_key)
        ms_values: list[float] = []

        for period in sorted_periods:
            value = history[period]
            market_total = market_period_totals.get((atc4_code, period), 0.0)
            ms = safe_div(value, market_total)
            ms_pct = ms * 100 if ms is not None else None
            if ms_pct is not None:
                ms_values.append(ms_pct)
            prev = value_at(history, prev_month(period))
            prev_q = value_at(history, prev_quarter_month(period))
            prev_y = value_at(history, same_month_prev_year(period))
            growth_abs = value - prev_y if prev_y is not None else None
            market_prev_y = value_at(atc_history, same_month_prev_year(period))
            market_growth_abs = atc_history.get(period) - market_prev_y if market_prev_y is not None else None
            gc, gc_warning = compute_growth_contribution(growth_abs, market_growth_abs)
            cagr_5y = cagr_from_history(history, period, 5)
            market_cagr_5y = cagr_from_history(atc_history, period, 5)
            ei_5y, ei_warning = compute_ei(cagr_5y, market_cagr_5y)
            metric_history[period] = {
                "raw_value": value,
                "ms": ms_pct,
                "mom": pct_growth(value, prev),
                "qoq": pct_growth(value, prev_q),
                "yoy": pct_growth(value, prev_y),
                "mat": mat_growth(history, period),
                "growth_abs": growth_abs,
                "rank": ranks.get((str(brand_key), period)),
            }
            extended_history[period] = {
                "cagr_1y": cagr_from_history(history, period, 1),
                "cagr_3y": cagr_from_history(history, period, 3),
                "cagr_5y": cagr_5y,
                "ei_5y": ei_5y,
                "momentum_score": compute_momentum(ms_values[-4:]) if len(ms_values) >= 4 else None,
                "growth_contribution": gc,
                "growth_contribution_pct": gc,
                "hhi": hhi_by_atc_period.get((str(atc4_code), period)),
                "market_cagr_5y": market_cagr_5y,
                "warnings": [w for w in (gc_warning, ei_warning) if w],
            }

        first = group.iloc[0]
        by_dimension = {
            "class": first.get("class"),
            "molecule": first.get("molecule"),
            "dosage_form": first.get("dosage_form"),
            "strength_tier": first.get("strength_pack"),
            "nhi_type": first.get("nhi_type"),
            "ox_gx": first.get("ox_gx"),
            "fish_oil": first.get("fish_oil"),
            "company": first.get("판매사"),
            "manufacturer": first.get("제조사"),
            "source_table": sorted(group["source_table"].dropna().astype(str).unique().tolist()),
        }
        rows.append(
            {
                "brand_key": str(brand_key),
                "brand_id": None if pd.isna(brand_id) else str(brand_id),
                "brand_name": str(first.get("brand_name") or brand_key),
                "atc4_code": str(atc4_code),
                "atc4_desc": first.get("atc4_desc"),
                "source": source,
                "measure": measure,
                "unit_label": UNIT_LABELS[(source, measure)],
                "metric_history": metric_history,
                "extended_metric_history": extended_history,
                "channel_data": build_channel_or_specialty_data(group, value_col, "channel"),
                "specialty_data": build_channel_or_specialty_data(group, value_col, "specialty"),
                "by_dimension": by_dimension,
                "raw_value_history": history,
                "payload": {
                    "phase": "16-G-4-Fix-ETL-v3",
                    "dry_run": True,
                    "row_count": int(len(group)),
                    "period_count": int(len(history)),
                },
            }
        )
    return rows


def build_market_rows(source: str, measure: str, df: pd.DataFrame, value_col: str) -> list[dict[str, Any]]:
    working = df.loc[df[value_col].notna() & (df[value_col] > 0)].copy()
    if working.empty:
        return []
    rows: list[dict[str, Any]] = []
    for atc4_code, group in working.groupby("atc4_code", dropna=False):
        market_size_series = period_value_map(group, value_col)
        hhi_series = {
            str(period): hhi_for_period(part, value_col)
            for period, part in group.groupby("period_yyyymm", dropna=False)
        }
        brand_ranking: dict[str, list[dict[str, Any]]] = {}
        for period, part in group.groupby("period_yyyymm", dropna=False):
            agg = (
                part.groupby(["brand_key", "brand_name"], dropna=False)[value_col]
                .sum()
                .reset_index(name="raw_value")
                .sort_values("raw_value", ascending=False)
                .reset_index(drop=True)
            )
            total = agg["raw_value"].sum()
            agg["rank"] = agg.index + 1
            agg["ms"] = agg["raw_value"].map(lambda v: safe_div(v, total) * 100 if total else None)
            brand_ranking[str(period)] = agg.head(20).to_dict(orient="records")

        atc4_desc = next((v for v in group["atc4_desc"].dropna().astype(str).unique().tolist() if v), None)
        rows.append(
            {
                "atc4_code": str(atc4_code),
                "atc4_desc": atc4_desc,
                "source": source,
                "measure": measure,
                "unit_label": UNIT_LABELS[(source, measure)],
                "market_size_series": market_size_series,
                "hhi_series": hhi_series,
                "brand_ranking": brand_ranking,
                "company_ranking_stacked": {},
                "company_concentration_trend": {},
                "ei_ms_matrix": [],
                "growth_contribution_ms_matrix": [],
                "growth_contribution": {},
                "analysis_levels": {},
                "level_top5_trend": {},
                "target_customer_competition": {},
                "payload": {
                    "phase": "16-G-4-Fix-ETL-v3",
                    "dry_run": True,
                    "brand_count": int(group["brand_key"].nunique()),
                    "period_count": int(len(market_size_series)),
                },
            }
        )
    return rows


def restrict_atc4(df: pd.DataFrame, limit_atc4: int | None) -> pd.DataFrame:
    if not limit_atc4:
        return df
    atc4_values = sorted(v for v in df["atc4_code"].dropna().unique().tolist() if v != "UNKNOWN")[:limit_atc4]
    if not atc4_values:
        atc4_values = sorted(df["atc4_code"].dropna().unique().tolist())[:limit_atc4]
    return df.loc[df["atc4_code"].isin(atc4_values)].copy()


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(dumps(row) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def compute_general(source: str, dry_run: bool, limit_atc4: int | None, max_rows: int, ml: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    if not dry_run:
        raise RuntimeError("Phase 16-G-4-Fix-ETL-v3 is dry-run only; INSERT is deferred to Fix-Load")

    frame = prepare_general_frame(source, max_rows=max_rows, ml=ml)
    if frame.empty:
        return [], [], {"source": source, "input_rows": 0}
    before_atc = len(frame)
    frame = restrict_atc4(frame, limit_atc4)
    LOGGER.info("[%s] source rows=%s after_atc_filter=%s", source, f"{before_atc:,}", f"{len(frame):,}")

    brand_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    for measure in MEASURES_BY_SOURCE[source]:
        value_col = MEASURE_COLUMN_MAP[(source, measure)]
        if value_col not in frame.columns:
            LOGGER.warning("[%s/%s] missing value column %s", source, measure, value_col)
            continue
        frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
        brand_rows.extend(build_brand_rows(source, measure, frame, value_col))
        market_rows.extend(build_market_rows(source, measure, frame, value_col))

    if dry_run:
        write_jsonl(general_brand_jsonl_path(source, ml=ml), brand_rows)
        write_jsonl(general_market_jsonl_path(source, ml=ml), market_rows)

    stats = {
        "source": source,
        "input_rows": int(before_atc),
        "used_rows": int(len(frame)),
        "atc4_count": int(frame["atc4_code"].nunique()),
        "brand_rows": len(brand_rows),
        "market_rows": len(market_rows),
        "measures": list(MEASURES_BY_SOURCE[source]),
    }
    return brand_rows, market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=ALLOWED_SOURCES)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit-atc4", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=250_000)
    parser.add_argument("--ml", help="Optional ml_id source filter for fast dry-run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.source:
        raise SystemExit("--all and --source are mutually exclusive")
    if not args.all and not args.source:
        raise SystemExit("Provide --source SOURCE or --all")
    sources = list(ALLOWED_SOURCES) if args.all else [args.source]
    for source in sources:
        brand_rows, market_rows, stats = compute_general(
            source=source,
            dry_run=args.dry_run,
            limit_atc4=args.limit_atc4,
            max_rows=args.max_rows,
            ml=args.ml,
        )
        print(f"\n=== {source} general dry-run ===")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if brand_rows:
            print("sample brand row:")
            print(json.dumps(json_ready(brand_rows[0]), ensure_ascii=False)[:1200])
        if market_rows:
            print("sample market row:")
            print(json.dumps(json_ready(market_rows[0]), ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
