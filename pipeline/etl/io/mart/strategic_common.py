from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable
import json
import os

import pandas as pd

from pipeline.etl.io.enrich.normalize import normalize_atc

from .brand_key_normalize import normalize_brand_name
from .general_config import ALLOWED_SOURCES, GENERAL_BRAND_INSERT_COLUMNS, JSON_INSERT_COLUMNS, mariadb_connect
from .general_json import dumps
from .strategic_constants import IQVIA_MEASURES, UBIST_MEASURES


def notna(value: Any) -> bool:
    try:
        return not bool(pd.isna(value))
    except (TypeError, ValueError):
        return value is not None


def truthy(value: Any) -> bool:
    if not notna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def parse_json_list(value: Any) -> list[str]:
    if not notna(value):
        return []
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError(f"expected JSON list, found={text!r}")
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def atc4_aliases(value: Any) -> set[str]:
    text = str(value or "").strip().upper()
    if not text:
        return set()
    aliases = {text, normalize_atc(text).upper()}
    for code in list(aliases):
        if len(code) >= 4 and code[0].isalpha() and code[1] == "0" and code[2].isdigit():
            aliases.add(code[0] + code[2:])
    for code in list(aliases):
        if len(code) == 4 and code[-1] == "0" and code[0].isalpha() and code[1].isdigit() and code[2].isalpha():
            aliases.add(code[:-1])
        if len(code) == 5 and code[-1] == "0" and code[0].isalpha() and code[1:3].isdigit() and code[3].isalpha():
            aliases.add(code[:-1])
    return {code for code in aliases if code}


def allowed_atc4_aliases(allowed_atc4_codes: Iterable[str]) -> set[str]:
    aliases: set[str] = set()
    for code in allowed_atc4_codes:
        aliases.update(atc4_aliases(code))
    return aliases


def allowed_atc4_codes(overlay: dict[str, Any], market_row: pd.Series) -> set[str]:
    allowed = set(parse_json_list(overlay.get("allowed_atc4_codes_json")))
    return allowed or set(parse_json_list(market_row.get("atc_codes_json")))


def row_atc4_code(row: dict[str, Any]) -> str:
    return str(row.get("atc4_code") or "").strip().upper()


def expected_measure_pairs(data_source: Any) -> set[tuple[str, str]]:
    value = str(data_source or "").strip().lower()
    expected: set[tuple[str, str]] = set()
    if value in {"ubist", "both", "dual"}:
        expected.update(("ubist", measure) for measure in UBIST_MEASURES)
    if value in {"iqvia", "iqvia_nsa", "both", "dual"}:
        expected.update(("iqvia_nsa", measure) for measure in IQVIA_MEASURES)
    if not expected:
        raise RuntimeError(f"Unsupported strategic data_source={data_source!r}")
    return expected


def drop_strict_excluded_rows(brands: pd.DataFrame, label: str) -> pd.DataFrame:
    if "is_excluded" not in brands.columns:
        return brands
    excluded_mask = brands["is_excluded"].map(truthy)
    removed = int(excluded_mask.sum())
    if removed:
        print(f"[exclude] strict 제외 제거 ({label}): {len(brands)} -> {len(brands) - removed}")
    return brands.loc[~excluded_mask].copy()


def is_jw_name(name: Any) -> bool:
    text = str(name or "")
    return any(token in text for token in ("리바로", "가드", "라베칸", "제이클", "타발리스", "시그마트", "악템라", "페린젝트", "베노훼럼", "헴리브라", "엔커버", "위너프", "플라주오피"))


def catalog_by_key(brands: pd.DataFrame) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    brands = brands.copy()
    if "is_jw" not in brands.columns:
        brands["is_jw"] = False
    brands["_jw_sort"] = brands["is_jw"].map(truthy).astype(int)
    brands = brands.sort_values(["_jw_sort", "brand_id"], ascending=[False, True])
    for key, part in brands.groupby("brand_key", dropna=False):
        if not key:
            continue
        first = part.iloc[0].to_dict()
        first["catalog_brand_ids"] = part["brand_id"].astype(str).tolist()
        first["catalog_names"] = part["name"].astype(str).tolist()
        if "allowed_atc4_codes_json" in part.columns:
            allowed: set[str] = set()
            for value in part["allowed_atc4_codes_json"]:
                allowed.update(parse_json_list(value))
            first["allowed_atc4_codes_json"] = json.dumps(sorted(allowed), ensure_ascii=False) if allowed else None
        if "is_class_excluded" in part.columns:
            first["is_class_excluded"] = bool(part["is_class_excluded"].map(truthy).any())
        grouped[str(key)] = first
    return grouped


def display_brand_name(row: dict[str, Any], overlay: dict[str, Any]) -> str:
    if truthy(overlay.get("is_jw")):
        canonical_name = overlay.get("canonical_name")
        if notna(canonical_name) and str(canonical_name).strip():
            return str(canonical_name)
        return str(overlay.get("name") or row.get("brand_name") or row.get("brand_key") or "")
    return str(row.get("brand_name") or row.get("brand_key") or overlay.get("name") or "")


def output_brand_key(row: dict[str, Any], overlay: dict[str, Any], display_name: str) -> str:
    if truthy(overlay.get("is_jw")):
        return display_name
    return str(row.get("brand_key") or normalize_brand_name(display_name))


def fetch_general_rows_from_db(source: str | None = None, atc4_codes: set[str] | None = None) -> list[dict[str, Any]]:
    source_db = os.environ.get("MARIADB_SOURCE_DATABASE") or os.environ.get("MARIADB_DATABASE") or "jw_mart"
    clauses: list[str] = []
    params: list[str] = []
    if source:
        clauses.append("source=%s")
        params.append(source)
    if atc4_codes:
        codes = sorted(atc4_codes)
        clauses.append("atc4_code IN (" + ",".join(["%s"] * len(codes)) + ")")
        params.extend(codes)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    sql = "SELECT " + ",".join(GENERAL_BRAND_INSERT_COLUMNS) + f" FROM `{source_db}`.mart_general_brand_metric " + where
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()
    json_columns = set(JSON_INSERT_COLUMNS) | {"metric_history", "extended_metric_history"}
    for row in rows:
        for col in GENERAL_BRAND_INSERT_COLUMNS:
            if col in json_columns:
                row[col] = json.loads(row[col]) if row.get(col) else {}
        # Strategic UBIST rows derive their runtime channel contract from this
        # verified general mart facility-specialty matrix; do not clear it here.
    return rows


def load_general_rows(source: str, atc4_codes: set[str] | None = None) -> list[dict[str, Any]]:
    rows = fetch_general_rows_from_db(source, atc4_codes)
    if not rows:
        raise RuntimeError(f"DB returned no {source} general rows")
    return rows


def insert_rows(table: str, columns: list[str], rows: list[dict[str, Any]], unique_cols: set[str], batch_size: int = 500) -> None:
    if not rows:
        return
    placeholders = ",".join(["%s"] * len(columns))
    update_sql = ",".join([f"{col}=VALUES({col})" for col in columns if col not in unique_cols])
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"
    payloads = [
        tuple(dumps(row.get(col)) if col in JSON_INSERT_COLUMNS else row.get(col) for col in columns)
        for row in rows
    ]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for start in range(0, len(payloads), batch_size):
                cur.executemany(sql, payloads[start : start + batch_size])
    finally:
        conn.close()


def delete_existing_rows(table: str, market_col: str, market_ids: set[str]) -> None:
    if not market_ids:
        return
    placeholders = ",".join(["%s"] * len(market_ids))
    sql = f"DELETE FROM {table} WHERE {market_col} IN ({placeholders})"
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(sorted(market_ids)))
    finally:
        conn.close()


def merge_numeric_json_values(left: Any, right: Any) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merge_numeric_json_values(merged[key], value) if key in merged else deepcopy(value)
        return merged
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
        return float(left) + float(right)
    return deepcopy(left) if left not in (None, {}, []) else deepcopy(right)


def sum_raw_histories(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows:
        for period, value in (row.get("raw_value_history") or {}).items():
            try:
                totals[str(period)] = totals.get(str(period), 0.0) + float(value or 0)
            except (TypeError, ValueError):
                continue
    return dict(sorted(totals.items()))
