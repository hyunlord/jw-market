#!/usr/bin/env python3
"""Shared helpers for Phase 2 spec-aligned cache builders."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from layer3_compute_general_v3 import dumps, json_ready, mariadb_connect, safe_float

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"

SOURCE_TO_API = {"ubist": "UBIST", "iqvia_nsa": "IQVIA"}
API_TO_SOURCE = {v: k for k, v in SOURCE_TO_API.items()}
MEASURES_BY_SOURCE = {
    "ubist": ("sales", "volume"),
    "iqvia_nsa": ("sales", "unit", "dosage_unit", "counting_unit"),
}


def optional_float(value: Any) -> float | None:
    """Parse a numeric value while preserving missing/uncomputable values as None."""
    try:
        if value is None or pd.isna(value):
            return None
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def period_value(item: Any) -> float | None:
    """Extract the numeric value from a period payload without inventing zero."""
    if isinstance(item, dict):
        for key in ("raw_value", "value", "market_size", "sales"):
            if key in item:
                return optional_float(item[key])
        return None
    return optional_float(item)


def annual_totals(series: dict[str, Any] | None) -> list[tuple[int, float]]:
    """Aggregate monthly or quarterly period series into sorted yearly totals."""
    totals: dict[int, float] = {}
    for period, item in (series or {}).items():
        key = period_key(str(period))
        year = key[0]
        if year <= 0:
            continue
        value = period_value(item)
        if value is None:
            continue
        totals[year] = totals.get(year, 0.0) + value
    return sorted(totals.items())


def calculate_cagr_v2(start_value: Any, end_value: Any, years: float | int | None) -> float | None:
    """CAGR using PL-defined zero handling.

    Cases:
    - start > 0, end > 0: standard CAGR
    - start > 0, end = 0: -100%
    - start = 0, end > 0: not computable from this basis
    - start = 0, end = 0: 0%
    """
    start = optional_float(start_value)
    end = optional_float(end_value)
    span = optional_float(years)
    if start is None or end is None or span is None or span <= 0:
        return None
    if start == 0 and end == 0:
        return 0.0
    if start == 0 and end > 0:
        return None
    if start > 0 and end == 0:
        return -100.0
    if start < 0 or end < 0:
        return None
    return ((end / start) ** (1 / span) - 1) * 100


def _window_start(annual: list[tuple[int, float]], end_year: int, target_years: int) -> tuple[int, float] | None:
    by_year = dict(annual)
    wanted_year = end_year - target_years
    if wanted_year in by_year:
        return wanted_year, by_year[wanted_year]
    earlier = [(year, value) for year, value in annual if year < end_year]
    return earlier[0] if earlier else None


def calculate_ei_with_fallback(
    brand_series: dict[str, Any] | None,
    market_series: dict[str, Any] | None,
    target_years: int = 5,
) -> dict[str, Any]:
    """Calculate EI on the 5-year basis, preserving pre-launch as uncomputable.

    PL originally considered a one-year fallback for pre-launch brands, then
    reversed that decision: if the 5-year brand CAGR cannot be calculated
    because the start value is zero and the end value is positive, EI should
    remain N/A rather than switching bases.
    """
    brand_annual = annual_totals(brand_series)
    market_annual = annual_totals(market_series)
    if len(brand_annual) < 2 or len(market_annual) < 2:
        return {"ei": None, "basis": "no_data", "note": "insufficient history"}

    end_year, brand_end = brand_annual[-1]
    market_by_year = dict(market_annual)
    if end_year not in market_by_year:
        return {"ei": None, "basis": "no_data", "note": "missing market end period"}
    market_end = market_by_year[end_year]

    brand_start_pair = _window_start(brand_annual, end_year, target_years)
    market_start_pair = _window_start(market_annual, end_year, target_years)
    if not brand_start_pair or not market_start_pair:
        return {"ei": None, "basis": "no_data", "note": "insufficient history"}

    brand_start_year, brand_start = brand_start_pair
    market_start_year, market_start = market_start_pair
    standard_years = max(end_year - brand_start_year, 1)
    market_years = max(end_year - market_start_year, 1)
    brand_cagr = calculate_cagr_v2(brand_start, brand_end, standard_years)
    market_cagr = calculate_cagr_v2(market_start, market_end, market_years)

    if brand_cagr is not None and market_cagr is not None and market_cagr != 0:
        return {
            "ei": round((brand_cagr / market_cagr) * 100, 4),
            "basis": "standard_5y",
            "period_years": target_years,
            "brand_cagr_pct": round(brand_cagr, 4),
            "market_cagr_pct": round(market_cagr, 4),
        }

    return {"ei": None, "basis": "unable", "note": "5년 전 매출 0 — N/A"}


UNIT_LABELS = {
    ("ubist", "sales"): "KRW",
    ("ubist", "volume"): "Rx",
    ("iqvia_nsa", "sales"): "KRW",
    ("iqvia_nsa", "unit"): "unit",
    ("iqvia_nsa", "dosage_unit"): "dosage unit",
    ("iqvia_nsa", "counting_unit"): "counting unit",
}
CANONICAL_25 = {
    "라베칸",
    "라베칸듀오",
    "제이클",
    "가드렛",
    "가드메트",
    "타발리스",
    "시그마트",
    "리바로",
    "리바로젯",
    "리바로페노",
    "리바로하이",
    "리바로브이",
    "트루패스",
    "피나스타",
    "제이다트",
    "뉴트로진",
    "모빌리아",
    "악템라",
    "페린젝트",
    "베노훼럼",
    "헴리브라",
    "위너프",
    "위너프A+",
    "엔커버",
    "플라주오피",
}


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--output-db", default="jw_mart", help="Compatibility option; DB comes from docker env.")
    p.add_argument("--verbose", action="store_true")
    return p


def load_catalog(name: str) -> pd.DataFrame:
    return pd.read_parquet(CATALOG_DIR / name / f"{name}.parquet")


def ml_to_strategy(ml_id: str | None) -> str | None:
    if not ml_id:
        return None
    match = re.search(r"(\d+)$", str(ml_id))
    return f"strategy_{int(match.group(1)):03d}" if match else str(ml_id)


def api_source(source: str | None) -> str:
    return SOURCE_TO_API.get(str(source or ""), str(source or "").upper())


def source_list(data_source: Any) -> list[str]:
    text = str(data_source or "").lower()
    if text in {"both", "dual", "ubist+iqvia", "iqvia+ubist"}:
        return ["UBIST", "IQVIA"]
    if "ubist" in text and "iqvia" in text:
        return ["UBIST", "IQVIA"]
    if "iqvia" in text:
        return ["IQVIA"]
    if "ubist" in text:
        return ["UBIST"]
    return []


def payload_size(payload: Any) -> int:
    return len(dumps(payload).encode("utf-8"))


def decode_json(value: Any) -> Any:
    if value in (None, ""):
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=None)
def period_key(value: str) -> tuple[int, int, int, str]:
    text = str(value)
    match = re.match(r"^(\d{4})-(\d{2})$", text)
    if match:
        return (int(match.group(1)), int(match.group(2)), 0, text)
    match = re.match(r"^(\d{4})-Q([1-4])$", text)
    if match:
        return (int(match.group(1)), int(match.group(2)) * 3, 1, text)
    return (0, 0, 0, text)


def latest_pair(series: dict[str, Any] | None) -> tuple[str | None, Any]:
    data = series or {}
    if not data:
        return None, {}
    key = sorted(data.keys(), key=period_key)[-1]
    return key, data[key]


def first_pair(series: dict[str, Any] | None) -> tuple[str | None, Any]:
    data = series or {}
    if not data:
        return None, {}
    key = sorted(data.keys(), key=period_key)[0]
    return key, data[key]


def series_latest_number(series: dict[str, Any] | None) -> float | None:
    _, item = latest_pair(series)
    if isinstance(item, dict):
        for key in ("raw_value", "value", "market_size", "sales"):
            if key in item:
                return safe_float(item[key])
        return None
    if item in ({}, None):
        return None
    return safe_float(item)


def series_cagr(series: dict[str, Any] | None) -> float | None:
    data = series or {}
    if len(data) < 2:
        return None
    first_key, first_value = first_pair(data)
    last_key, last_value = latest_pair(data)
    first = period_value(first_value)
    last = period_value(last_value)
    if first is None or last is None or first_key is None or last_key is None:
        return None
    years = max((period_key(last_key)[0] - period_key(first_key)[0]), 1)
    cagr = calculate_cagr_v2(first, last, years)
    return round(cagr, 2) if cagr is not None else None


def fetch_all(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            return list(cur.fetchall())
    finally:
        conn.close()


def replace_rows(table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{c}`" for c in columns)
    sql = f"REPLACE INTO `{table}` ({names}) VALUES ({placeholders})"
    values = [tuple(row.get(col) for col in columns) for row in rows]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            # PyMySQL collapses executemany into one huge multi-row statement.
            # Phase 2 payloads can be hundreds of MB in aggregate, so keep each
            # cache row as an individual statement to avoid max-packet/memory
            # pressure while preserving autocommit semantics.
            for value in values:
                cur.execute(sql, value)
    finally:
        conn.close()


def display_ukrw(value: float | None) -> str:
    number = safe_float(value)
    return f"{number / 100_000_000:,.1f}억"


def numeric_mean(values: Iterable[Any]) -> float | None:
    nums = [safe_float(v) for v in values if v is not None]
    return round(sum(nums) / len(nums), 2) if nums else None


def metric_recent(history: dict[str, Any] | None) -> dict[str, Any]:
    _, item = latest_pair(history)
    return item if isinstance(item, dict) else {}


def metric_first(history: dict[str, Any] | None) -> dict[str, Any]:
    _, item = first_pair(history)
    return item if isinstance(item, dict) else {}


def dump_payload(payload: Any) -> str:
    return dumps(json_ready(payload))
