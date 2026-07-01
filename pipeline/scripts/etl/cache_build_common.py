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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from layer3_compute_general_v3 import dumps, json_ready, mariadb_connect, safe_float

try:
    import orjson
except ImportError:  # pragma: no cover - optional local speed-up
    orjson = None

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
        if value is None:
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


def _period_ordinal(period: str) -> tuple[int, int] | None:
    """Return (ordinal, periods_per_year) for monthly/quarterly period labels."""
    text = str(period)
    match = re.match(r"^(\d{4})-(\d{2})$", text)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        if 1 <= month <= 12:
            return year * 12 + (month - 1), 12
        return None
    match = re.match(r"^(\d{4})-Q([1-4])$", text)
    if match:
        year = int(match.group(1))
        quarter = int(match.group(2))
        return year * 4 + (quarter - 1), 4
    return None


def _endpoint_cagr(series: dict[str, Any] | None, years: int) -> dict[str, Any]:
    """CAGR using the same latest-vs-exact-endpoint rule as Layer3 marts.

    annual sum was rejected for Wave 3a because partial current-year totals
    (for example 2026 Jan-Apr) created fake negative CAGR/EI while the portal
    also displayed a separate positive CAGR. Cache now uses one domain rule:
    latest period vs exactly 5 years ago, falling back to 3 years only when
    the caller asks for that basis.
    """
    data = series or {}
    if len(data) < 2:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "note": "insufficient history"}
    latest_period, latest_item = latest_pair(data)
    latest_ord = _period_ordinal(str(latest_period)) if latest_period else None
    if latest_period is None or latest_ord is None:
        return {"cagr_pct": None, "basis": f"endpoint_{years}y", "period_years": years, "note": "invalid latest period"}
    ordinal, periods_per_year = latest_ord
    target_ordinal = ordinal - periods_per_year * years
    start_period = next(
        (str(period) for period in data if (_period_ordinal(str(period)) or (None, None))[0] == target_ordinal),
        None,
    )
    latest_value = period_value(latest_item)
    start_value = period_value(data.get(start_period)) if start_period else None
    if start_period is None or start_value is None:
        return {
            "cagr_pct": None,
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "start_period": start_period,
            "end_period": latest_period,
            "note": "missing endpoint period",
        }
    if start_value <= 0:
        return {
            "cagr_pct": None,
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "start_period": start_period,
            "end_period": latest_period,
            "note": "endpoint start value is not positive",
        }
    cagr = calculate_cagr_v2(start_value, latest_value, years)
    return {
        "cagr_pct": round(cagr, 4) if cagr is not None else None,
        "basis": f"endpoint_{years}y",
        "period_years": years,
        "start_period": start_period,
        "end_period": latest_period,
        "start_value": start_value,
        "end_value": latest_value,
    }


def calculate_ei_with_fallback(
    brand_series: dict[str, Any] | None,
    market_series: dict[str, Any] | None,
    target_years: int = 5,
) -> dict[str, Any]:
    """Calculate EI from one cache-wide endpoint CAGR policy.

    무엇/왜: 기존 annual_totals 경로는 2026년 4개월 partial-year를 최신
    연간값으로 사용해 표시 CAGR은 양수인데 EI용 market CAGR은 음수인
    분기를 만들었다. Wave 3a는 mart의 cagr_from_history와 같은 최신
    period-vs-exact-endpoint 기준으로 cache 표시값과 EI 값을 통일한다.
    도메인 근거: 5년 전 endpoint가 양수이면 5년 CAGR, 브랜드 5년 전이
    0/없으면 3년 endpoint로 fallback, 3년도 불가하면 N/A. annual-sum/MAT
    대안은 PL이 단일 endpoint를 선택했으므로 이번 cache-only fix에서는
    기각한다.
    """
    for years in (target_years, 3):
        brand_meta = _endpoint_cagr(brand_series, years)
        market_meta = _endpoint_cagr(market_series, years)
        brand_cagr = brand_meta.get("cagr_pct")
        market_cagr = market_meta.get("cagr_pct")
        if brand_cagr is None or market_cagr is None or market_cagr == 0:
            continue
        return {
            "ei": round((brand_cagr / market_cagr) * 100, 4),
            "basis": f"endpoint_{years}y",
            "period_years": years,
            "brand_cagr_pct": round(float(brand_cagr), 4),
            "market_cagr_pct": round(float(market_cagr), 4),
            "brand_start_period": brand_meta.get("start_period"),
            "brand_end_period": brand_meta.get("end_period"),
            "market_start_period": market_meta.get("start_period"),
            "market_end_period": market_meta.get("end_period"),
        }

    return {"ei": None, "basis": "endpoint_na", "note": "5년/3년 endpoint CAGR 산출 불가 — N/A"}


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


def load_catalog(name: str) -> Any:
    """Read a parquet catalog only for offline cache-build commands.

    API runtimes import this module for shared JSON/number helpers, but the
    slim backend image intentionally does not ship pandas or parquet files.
    Keeping pandas local to the catalog reader preserves the old offline
    builder behavior without reintroducing a runtime pandas dependency.
    """

    import pandas as pd

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
    """Display market CAGR using the same 5y→3y endpoint policy as EI."""
    for years in (5, 3):
        meta = _endpoint_cagr(series, years)
        cagr = meta.get("cagr_pct")
        if cagr is not None:
            return round(float(cagr), 2)
    return None


def fetch_all(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            return list(cur.fetchall())
    finally:
        conn.close()


def quote_table_name(table: str) -> str:
    parts = str(table or "").split(".")
    if not parts or len(parts) > 2:
        raise ValueError(f"unsafe table name: {table!r}")
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_]+", part):
            raise ValueError(f"unsafe table name: {table!r}")
    return ".".join(f"`{part}`" for part in parts)


def replace_rows(table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{c}`" for c in columns)
    sql = f"REPLACE INTO {quote_table_name(table)} ({names}) VALUES ({placeholders})"
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
    ready = json_ready(payload)
    if orjson is not None:
        return orjson.dumps(ready).decode("utf-8")
    return dumps(ready)
