from __future__ import annotations

from copy import deepcopy
from typing import Any

import duckdb
import pandas as pd

from .general_config import ENRICHED_DIR, SKU_DIMENSION_COLUMNS
from .general_history import fill_periods
from .general_utils import filter_ubist_aggregate_specialty_rows, ubist_channel_to_raw
from .layer3_normalize import prev_month, prev_quarter_month, same_month_prev_year
from .layer3_compute_extended import compute_ei, compute_growth_contribution, compute_momentum
from .general_history import cagr_from_history, mat_growth, pct_growth, value_at
from .ubist_channel_mapping import parse_channel_code


def clean_dimension_label(value: Any) -> str | None:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return None
    return text


def dimension_atoms(value: Any) -> list[str]:
    text = clean_dimension_label(value)
    if not text:
        return []
    return list(dict.fromkeys(part.strip() for part in text.split("|") if part.strip()))


def value_from_history_item(item: Any) -> float:
    if isinstance(item, dict):
        item = item.get("raw_value") or item.get("value") or item.get("sales")
    try:
        return float(item or 0.0)
    except (TypeError, ValueError):
        return 0.0


def series_from_history(history: Any) -> dict[str, dict[str, float]]:
    if not isinstance(history, dict):
        return {}
    return {str(period): {"raw_value": value_from_history_item(value)} for period, value in history.items()}


def rekey_dimension_field_to_label(row: dict[str, Any], field: str, label: Any) -> None:
    clean_label = clean_dimension_label(label)
    if not clean_label:
        return
    for payload_key in ("dimension_data", "dimension_channel_data", "dimension_specialty_data"):
        payload = row.get(payload_key)
        if not isinstance(payload, dict):
            continue
        field_bucket = payload.get(field)
        if not isinstance(field_bucket, dict) or not field_bucket or set(field_bucket) == {clean_label}:
            continue
        merged: dict[str, Any] = {}
        for series in field_bucket.values():
            from .strategic_common import merge_numeric_json_values

            merged = merge_numeric_json_values(merged, series)
        payload[field] = {clean_label: merged}
    by_dimension = row.get("by_dimension")
    if isinstance(by_dimension, dict):
        by_dimension[field] = clean_label


def clear_dimension_field(row: dict[str, Any], field: str) -> None:
    for payload_key in ("dimension_data", "dimension_channel_data", "dimension_specialty_data"):
        payload = row.get(payload_key)
        if isinstance(payload, dict):
            payload.pop(field, None)
    by_dimension = row.get("by_dimension")
    if isinstance(by_dimension, dict):
        by_dimension.pop(field, None)


def iqvia_recode_label(field: str, label: Any) -> str | None:
    clean_label = clean_dimension_label(label)
    if not clean_label or field not in {"dosage_form", "strength_pack"}:
        return clean_label
    atoms = dimension_atoms(clean_label)
    strength_raw = ("INFU", "C.T", "TAB", "CAP", "AMP", "LIQ", "PWD", "SYR", "SACH", "ORAL", "VIAL", "FILM", "GRAN", "SUSP", "V.SC", "PRE-F", "PREF", "PFS", "SRN", "DRY", "PLASTI", "BAG")
    dosage_raw = ("ORDINARY", "TABLET", "CAPSULE", "POWDER", "SOLUTION", "UNIT DOSE", "PARENTAL", "RETARD", "DRY", "VIAL", "BOTTLE", "INFUSION")
    markers = strength_raw if field == "strength_pack" else dosage_raw
    atoms = [atom for atom in atoms if atom.upper() not in {"NO STRENGTH", "NAN", "NONE", "NULL"} and not any(marker in atom.upper() for marker in markers)]
    return " | ".join(atoms) if atoms else None


def load_ubist_dimension_context(ml_id: str, strategic_products: pd.DataFrame) -> dict[str, Any]:
    enriched_path = ENRICHED_DIR / f"ml_id={ml_id}" / "data.parquet"
    if not enriched_path.exists() or strategic_products.empty:
        return {"code_dimensions": {}, "code_channel_history": {}, "code_specialty_history": {}, "stats": {"exists": enriched_path.exists()}}
    con = duckdb.connect()
    try:
        code_product = con.execute(f"SELECT DISTINCT split_part(source_row_id, '::', 6) AS product_code, product_id FROM read_parquet('{enriched_path}') WHERE source='ubist' AND source_row_id IS NOT NULL").df()
        raw_channel = con.execute(f"""SELECT product_code, channel, specialty, period_yyyymm, SUM(raw_sales) AS raw_sales, SUM(raw_volume) AS raw_volume FROM (
              SELECT DISTINCT source_row_id, split_part(source_row_id, '::', 6) AS product_code, channel, specialty, period_yyyymm,
                TRY_CAST(raw_rx_amt AS DOUBLE) AS raw_sales, TRY_CAST(raw_rx_qty AS DOUBLE) AS raw_volume
              FROM read_parquet('{enriched_path}') WHERE source='ubist' AND source_row_id IS NOT NULL
                AND (TRY_CAST(raw_rx_amt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_qty AS DOUBLE) > 0)) AS raw_rows GROUP BY 1,2,3,4""").df()
    finally:
        con.close()
    product_dims = strategic_products[[col for col in ["product_id", *SKU_DIMENSION_COLUMNS] if col in strategic_products.columns]].drop_duplicates("product_id")
    code_dims = code_product.merge(product_dims, on="product_id", how="left")
    code_dimensions: dict[str, dict[str, str]] = {}
    for product_code, part in code_dims.groupby("product_code", dropna=False):
        code = str(product_code or "").strip()
        if not code:
            continue
        for field in SKU_DIMENSION_COLUMNS:
            if field not in part.columns:
                continue
            atoms: set[str] = set()
            for value in part[field]:
                atoms.update(dimension_atoms(value))
            if len(atoms) == 1:
                code_dimensions.setdefault(code, {})[field] = next(iter(atoms))
    return _build_channel_context(raw_channel, code_dimensions)


def _display_specialty_channel(channel: Any, specialty: Any) -> str | None:
    facility = {"TH": "GH", "GH": "GH", "Semi": "GH", "CL": "CL", "기타": "OT", "OT": "OT"}.get(str(channel or "").strip())
    specialty_text = str(specialty or "").strip()
    if not facility or not specialty_text or specialty_text == "Unknown":
        return None
    try:
        parsed = parse_channel_code(f"{facility} {specialty_text}")
    except ValueError:
        return None
    return parsed.display_name if parsed else None


def _build_channel_context(raw_channel: pd.DataFrame, code_dimensions: dict[str, dict[str, str]]) -> dict[str, Any]:
    channel_history: dict[str, Any] = {"sales": {}, "volume": {}}
    specialty_history: dict[str, Any] = {"sales": {}, "volume": {}}
    filtered_channel = filter_ubist_aggregate_specialty_rows(raw_channel)
    for row in filtered_channel.to_dict("records"):
        code = str(row.get("product_code") or "").strip()
        if not code or code not in code_dimensions:
            continue
        channel = ubist_channel_to_raw(row.get("channel"))
        specialty_channel = _display_specialty_channel(row.get("channel"), row.get("specialty"))
        period = str(row.get("period_yyyymm") or "").strip()
        for field, label in code_dimensions[code].items():
            for measure, value_col in (("sales", "raw_sales"), ("volume", "raw_volume")):
                value = value_from_history_item(row.get(value_col))
                if value <= 0:
                    continue
                bucket = channel_history.setdefault(measure, {}).setdefault(code, {}).setdefault(field, {}).setdefault(label, {}).setdefault(channel, {})
                bucket[period] = {"raw_value": value_from_history_item(bucket.get(period)) + value}
                if specialty_channel:
                    sb = specialty_history.setdefault(measure, {}).setdefault(code, {}).setdefault(field, {}).setdefault(label, {}).setdefault(specialty_channel, {})
                    sb[period] = {"raw_value": value_from_history_item(sb.get(period)) + value}
    return {"code_dimensions": code_dimensions, "code_channel_history": channel_history, "code_specialty_history": specialty_history, "stats": {}}


def catalog_single_dimension_by_brand(catalog_rows: pd.DataFrame, strategic_products: pd.DataFrame) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    frames = [frame for frame in (strategic_products, catalog_rows) if frame is not None and not frame.empty and "brand_id" in frame.columns]
    if not frames:
        return result
    all_rows = pd.concat(frames, ignore_index=True, sort=False)
    for brand_id, part in all_rows.groupby("brand_id", dropna=False):
        brand_key = str(brand_id or "")
        for field in SKU_DIMENSION_COLUMNS:
            if field not in part.columns:
                continue
            atoms: set[str] = set()
            for value in part[field]:
                atoms.update(dimension_atoms(value))
            if len(atoms) == 1:
                result.setdefault(brand_key, {})[field] = next(iter(atoms))
    return result
