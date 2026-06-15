from __future__ import annotations

from copy import deepcopy
from typing import Any

from .general_config import SKU_DIMENSION_COLUMNS
from .iron_iv_dimensions import apply_iron_iv_dimension_rule, capture_iron_iv_source_strength
from .strategic_dimensions import (
    clean_dimension_label,
    iqvia_recode_label,
    rekey_dimension_field_to_label,
    series_from_history,
    value_from_history_item,
    clear_dimension_field,
)


def fill_dimension_series(target: dict[str, Any], existing: dict[str, Any], field: str, label: str, series: dict[str, Any]) -> None:
    if not label or not isinstance(series, dict):
        return
    label_bucket = target.setdefault(field, {}).setdefault(label, {})
    for period, item in series.items():
        if _field_period_has_existing_value(existing, field, str(period)):
            continue
        bucket = label_bucket.setdefault(str(period), {"raw_value": 0.0})
        bucket["raw_value"] = value_from_history_item(bucket) + value_from_history_item(item)


def fill_dimension_channel_series(target: dict[str, Any], existing: dict[str, Any], channel_totals: dict[str, Any], field: str, label: str, channel: str, series: dict[str, Any]) -> None:
    if not label or not channel or not isinstance(series, dict):
        return
    channel_bucket = target.setdefault(field, {}).setdefault(label, {}).setdefault(str(channel), {})
    for period, item in series.items():
        period_key = str(period)
        if _channel_period_total(channel_totals, str(channel), period_key) <= 0:
            continue
        if _field_channel_period_has_existing_value(existing, field, str(channel), period_key):
            continue
        bucket = channel_bucket.setdefault(period_key, {"raw_value": 0.0})
        bucket["raw_value"] = value_from_history_item(bucket) + value_from_history_item(item)


def fill_dimension_specialty_series(target: dict[str, Any], field: str, label: str, specialty_channel: str, series: dict[str, Any]) -> None:
    if not label or not specialty_channel or not isinstance(series, dict):
        return
    bucket = target.setdefault(field, {}).setdefault(label, {}).setdefault(str(specialty_channel), {})
    for period, item in series.items():
        period_bucket = bucket.setdefault(str(period), {"raw_value": 0.0})
        period_bucket["raw_value"] = value_from_history_item(period_bucket) + value_from_history_item(item)


def enhance_strategic_dimensions(row: dict[str, Any], context: dict[str, Any], *, market_id: str | None = None) -> dict[str, Any]:
    capture_iron_iv_source_strength(row, market_id=market_id)
    dimension_data = deepcopy(row.get("dimension_data") or {})
    dimension_channel_data = deepcopy(row.get("dimension_channel_data") or {})
    dimension_specialty_data = deepcopy(row.get("dimension_specialty_data") or {})
    existing_dimension_data = deepcopy(dimension_data)
    existing_dimension_channel_data = deepcopy(dimension_channel_data)
    by_dimension = deepcopy(row.get("by_dimension") or {})
    brand_single = context.get("brand_single_dimensions", {}).get(str(row.get("brand_id") or ""), {})
    code_dimensions = context.get("code_dimensions", {})
    code_channel_history = context.get("code_channel_history", {}).get(str(row.get("measure") or ""), {})
    code_specialty_history = context.get("code_specialty_history", {}).get(str(row.get("measure") or ""), {})
    row_history = series_from_history(row.get("raw_value_history"))
    channel_data = row.get("channel_data") if isinstance(row.get("channel_data"), dict) else {}
    products = by_dimension.get("products") if isinstance(by_dimension.get("products"), list) else []
    is_iqvia = str(row.get("source") or "").strip().lower() == "iqvia_nsa"
    for field in SKU_DIMENSION_COLUMNS:
        overlay_data = row.get("overlay_data") if isinstance(row.get("overlay_data"), dict) else {}
        label = brand_single.get(field) or clean_dimension_label(overlay_data.get(field))
        if is_iqvia and field in {"dosage_form", "strength_pack"}:
            label = iqvia_recode_label(field, label)
            if not label:
                clear_dimension_field(row, field)
                dimension_data = deepcopy(row.get("dimension_data") or {})
                dimension_channel_data = deepcopy(row.get("dimension_channel_data") or {})
                dimension_specialty_data = deepcopy(row.get("dimension_specialty_data") or {})
                existing_dimension_data = deepcopy(dimension_data)
                existing_dimension_channel_data = deepcopy(dimension_channel_data)
                by_dimension = deepcopy(row.get("by_dimension") or {})
                continue
        if label:
            rekey_dimension_field_to_label(row, field, label)
            dimension_data = deepcopy(row.get("dimension_data") or {})
            dimension_channel_data = deepcopy(row.get("dimension_channel_data") or {})
            dimension_specialty_data = deepcopy(row.get("dimension_specialty_data") or {})
            fill_dimension_series(dimension_data, existing_dimension_data, field, label, row_history)
            for channel, series in channel_data.items():
                fill_dimension_channel_series(dimension_channel_data, existing_dimension_channel_data, channel_data, field, label, str(channel), series)
            by_dimension = deepcopy(row.get("by_dimension") or {})
            if not clean_dimension_label(by_dimension.get(field)):
                by_dimension[field] = label
        for product in products:
            if not isinstance(product, dict):
                continue
            code = str(product.get("product_code") or "").strip()
            product_label = code_dimensions.get(code, {}).get(field) or label
            if not product_label:
                continue
            if not label:
                product_history = series_from_history(product.get("raw_value_history"))
                fill_dimension_series(dimension_data, existing_dimension_data, field, product_label, product_history)
                channel_map = (((code_channel_history.get(code) or {}).get(field) or {}).get(product_label) or {})
                for channel, series in channel_map.items():
                    fill_dimension_channel_series(dimension_channel_data, existing_dimension_channel_data, channel_data, field, product_label, str(channel), series)
            specialty_map = (((code_specialty_history.get(code) or {}).get(field) or {}).get(product_label) or {})
            for specialty_channel, series in specialty_map.items():
                fill_dimension_specialty_series(dimension_specialty_data, field, product_label, str(specialty_channel), series)
    row.update({"dimension_data": dimension_data, "dimension_channel_data": dimension_channel_data, "dimension_specialty_data": dimension_specialty_data, "by_dimension": by_dimension})
    return apply_iron_iv_dimension_rule(row, market_id=market_id)


def apply_cd_dimension_recode(row: dict[str, Any], overlay: dict[str, Any], *, market_id: str | None) -> dict[str, Any]:
    capture_iron_iv_source_strength(row, market_id=market_id)
    is_iqvia = str(row.get("source") or "").strip().lower() == "iqvia_nsa"
    for field in ("molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"):
        label = overlay.get(field)
        if is_iqvia and field in {"dosage_form", "strength_pack"}:
            label = iqvia_recode_label(field, label)
            if not label:
                clear_dimension_field(row, field)
                continue
        rekey_dimension_field_to_label(row, field, label)
    return apply_iron_iv_dimension_rule(row, market_id=market_id)


def _field_period_has_existing_value(existing: dict[str, Any], field: str, period: str) -> bool:
    field_bucket = existing.get(field) if isinstance(existing, dict) else None
    return any(isinstance(series, dict) and value_from_history_item(series.get(period)) > 0 for series in (field_bucket or {}).values())


def _field_channel_period_has_existing_value(existing: dict[str, Any], field: str, channel: str, period: str) -> bool:
    field_bucket = existing.get(field) if isinstance(existing, dict) else None
    for label_channels in (field_bucket or {}).values():
        channel_series = label_channels.get(channel) if isinstance(label_channels, dict) else None
        if isinstance(channel_series, dict) and value_from_history_item(channel_series.get(period)) > 0:
            return True
    return False


def _channel_period_total(channel_totals: dict[str, Any], channel: str, period: str) -> float:
    series = channel_totals.get(channel) if isinstance(channel_totals, dict) else None
    return value_from_history_item(series.get(period)) if isinstance(series, dict) else 0.0
