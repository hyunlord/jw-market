#!/usr/bin/env python3
"""Shared helpers for Phase 2 spec-aligned cache builders."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
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
    first = safe_float(first_value.get("raw_value") if isinstance(first_value, dict) else first_value)
    last = safe_float(last_value.get("raw_value") if isinstance(last_value, dict) else last_value)
    if first <= 0 or last <= 0 or first_key is None or last_key is None:
        return None
    years = max((period_key(last_key)[0] - period_key(first_key)[0]), 1)
    return round(((last / first) ** (1 / years) - 1) * 100, 2)


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
