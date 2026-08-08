#!/usr/bin/env python3
"""Shared helpers for Phase 2 spec-aligned cache builders."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
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

try:
    from pydantic_core import from_json as pydantic_json_loads
except ImportError:  # pragma: no cover - ETL-only environments may omit Pydantic
    pydantic_json_loads = None

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


@lru_cache(maxsize=512)
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


def endpoint_cagr(series: dict[str, Any] | None, years: int) -> dict[str, Any]:
    """CAGR using the same latest-vs-exact-endpoint rule as Layer3 marts.

    annual sum was rejected for Wave 3a because partial current-year totals
    (for example 2026 Jan-Apr) created fake negative CAGR/EI while the portal
    also displayed a separate positive CAGR. Cache now uses one domain rule:
    latest period vs exactly 5 years ago, falling back to 3 years only when
    the caller asks for that basis. IQVIA's 20-quarter display contract has
    19 elapsed quarter intervals, so only that quarterly source shape accepts
    the first point using its actual 4.75-year span. Monthly UBIST requires the
    exact 60-month-prior point and never substitutes a 59-month span.
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
    start_period = _period_at_ordinal(data, target_ordinal)
    start_value = period_value(data.get(start_period)) if start_period else None
    period_years: float | int = years
    if start_value is None and years == 5 and periods_per_year == 4:
        elapsed_periods = periods_per_year * years - 1
        start_period = _period_at_ordinal(data, ordinal - elapsed_periods)
        start_value = period_value(data.get(start_period)) if start_period else None
        period_years = elapsed_periods / periods_per_year
    latest_value = period_value(latest_item)
    if start_period is None or start_value is None:
        return {
            "cagr_pct": None,
            "basis": f"endpoint_{years}y",
            "period_years": period_years,
            "start_period": start_period,
            "end_period": latest_period,
            "note": "missing endpoint period",
        }
    if start_value <= 0:
        return {
            "cagr_pct": None,
            "basis": f"endpoint_{years}y",
            "period_years": period_years,
            "start_period": start_period,
            "end_period": latest_period,
            "note": "endpoint start value is not positive",
        }
    cagr = calculate_cagr_v2(start_value, latest_value, period_years)
    return {
        "cagr_pct": round(cagr, 4) if cagr is not None else None,
        "basis": f"endpoint_{years}y",
        "period_years": period_years,
        "start_period": start_period,
        "end_period": latest_period,
        "start_value": start_value,
        "end_value": latest_value,
    }


def _period_at_ordinal(series: dict[str, Any], target_ordinal: int) -> str | None:
    return next(
        (
            str(period)
            for period in series
            if (_period_ordinal(str(period)) or (None, None))[0] == target_ordinal
        ),
        None,
    )


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
        brand_meta = endpoint_cagr(brand_series, years)
        market_meta = endpoint_cagr(market_series, years)
        brand_cagr = brand_meta.get("cagr_pct")
        market_cagr = market_meta.get("cagr_pct")
        if brand_cagr is None or market_cagr is None or market_cagr == 0:
            continue
        if brand_meta.get("period_years") != market_meta.get("period_years"):
            continue
        return {
            "ei": round((brand_cagr / market_cagr) * 100, 4),
            "basis": f"endpoint_{years}y",
            "period_years": brand_meta.get("period_years"),
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


CATALOG_REQUIRED_COLUMNS = {
    "ml_market": {"ml_id", "atc_codes_json"},
    "cd_market": {"cd_id"},
    "strategic_brand": {"ml_id", "cd_id", "canonical_name", "is_excluded"},
}


def validate_catalog_schema(name: str, catalog: Any) -> None:
    required = CATALOG_REQUIRED_COLUMNS.get(name, set())
    missing = sorted(required - set(catalog.columns))
    if missing:
        raise RuntimeError(f"{name} catalog missing required columns: {missing}")


def active_catalog_member_rows(catalog: Any, field: str, market_id: str) -> list[dict[str, Any]]:
    """Return distinct, non-excluded strategic catalog members for a market."""
    if isinstance(catalog, list):
        candidates = catalog
    elif field in catalog.columns:
        candidates = [
            row.to_dict()
            for _, row in catalog[catalog[field].astype(str) == str(market_id)].iterrows()
        ]
    else:
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in candidates:
        if str(row.get(field) or "") != str(market_id):
            continue
        if int(safe_float(row.get("is_excluded")) or 0) != 0:
            continue
        name = str(row.get("canonical_name") or row.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(row)
    return result


def load_catalog(name: str) -> Any:
    """Read a parquet catalog only for offline cache-build commands.

    API runtimes import this module for shared JSON/number helpers, but the
    slim backend image intentionally does not ship pandas or parquet files.
    Keeping pandas local to the catalog reader preserves the old offline
    builder behavior without reintroducing a runtime pandas dependency.
    """

    import pandas as pd

    table = f"catalog_{name}"
    catalog = pd.DataFrame(fetch_all(f"SELECT * FROM {table}"))
    validate_catalog_schema(name, catalog)
    return catalog


def current_build_sha() -> str:
    configured = str(os.getenv("GIT_COMMIT") or os.getenv("APP_COMMIT_SHA") or "").strip()
    if configured:
        return configured
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def catalog_input_manifest(catalogs: dict[str, Any]) -> str:
    inputs: dict[str, dict[str, Any]] = {}
    for name, catalog in sorted(catalogs.items()):
        records = catalog.to_dict("records") if hasattr(catalog, "to_dict") else list(catalog)
        inputs[name] = {
            "row_count": len(records),
            "source_file_versions": sorted({
                str(row.get("source_file_version"))
                for row in records
                if row.get("source_file_version")
            }),
            "catalog_manifest_hashes": sorted({
                str(row.get("catalog_manifest_hash"))
                for row in records
                if row.get("catalog_manifest_hash")
            }),
            "ingested_at": sorted({
                str(row.get("ingested_at")) for row in records if row.get("ingested_at")
            }),
        }
    canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(
        {"inputs": inputs, "manifest_sha256": hashlib.sha256(canonical.encode()).hexdigest()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
    if orjson is not None:
        try:
            return orjson.loads(value)
        except (TypeError, orjson.JSONDecodeError):
            pass
    if pydantic_json_loads is not None:
        try:
            return pydantic_json_loads(value)
        except (TypeError, ValueError):
            pass
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
        meta = endpoint_cagr(series, years)
        cagr = meta.get("cagr_pct")
        if cagr is not None:
            return round(float(cagr), 2)
    return None


def _cagr_exclusive(
    series: dict[str, Any] | None,
    *,
    precision: int,
) -> tuple[float | None, float | None]:
    """Return exclusive endpoint CAGR slots at the requested JSON precision."""

    cagr_5y = endpoint_cagr(series, 5).get("cagr_pct")
    if cagr_5y is not None:
        return round(float(cagr_5y), precision), None
    cagr_3y = endpoint_cagr(series, 3).get("cagr_pct")
    if cagr_3y is not None:
        return None, round(float(cagr_3y), precision)
    return None, None


def market_cagr_exclusive(series: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Return ``(cagr_5y_pct, cagr_3y_pct)`` under an *exclusive* endpoint policy.

    Unlike :func:`series_cagr`, which silently returns a 3-year CAGR in the
    ``5y`` slot when the 5-year endpoint is missing, this reports the horizon
    explicitly so a consumer can tell which window a value describes:

    - 5-year endpoint computable          → ``(5y, None)``
    - 5-year missing, 3-year computable    → ``(None, 3y)``
    - neither computable                   → ``(None, None)``

    The two slots are never both non-null. ``None`` means "not computable" and
    must not be coerced to ``0``.
    """
    return _cagr_exclusive(series, precision=2)


def brand_cagr_exclusive(series: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Return exclusive brand CAGR slots without losing mart-level precision.

    Endpoint selection is intentionally shared with market CAGR, including the
    IQVIA-only 19-quarter substitute implemented by :func:`endpoint_cagr`.
    Monthly histories therefore keep their exact 5y/3y endpoint policy.
    """

    return _cagr_exclusive(series, precision=4)


def iqvia_period_to_display(period: str | None) -> str | None:
    """Convert a mart IQVIA quarter label ``YYYY-Qn`` to the portal ``YYYY-nQ``.

    Returns ``None`` when the input is missing or not a recognized quarter
    label, so callers surface "no data" rather than a malformed string.
    """
    if not period:
        return None
    match = re.match(r"^(\d{4})-Q([1-4])$", str(period))
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}Q"


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


def upsert_rows(table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Insert cache rows or update every supplied column on key conflicts."""
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{column}`" for column in columns)
    updates = ", ".join(f"`{column}` = VALUES(`{column}`)" for column in columns)
    sql = (
        f"INSERT INTO {quote_table_name(table)} ({names}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    values = [tuple(row.get(column) for column in columns) for row in rows]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for value in values:
                cur.execute(sql, value)
    finally:
        conn.close()


def insert_rows(table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Insert rows into a pre-validated empty staging table."""
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{c}`" for c in columns)
    sql = f"INSERT INTO {quote_table_name(table)} ({names}) VALUES ({placeholders})"
    values = [tuple(row.get(col) for col in columns) for row in rows]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
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
