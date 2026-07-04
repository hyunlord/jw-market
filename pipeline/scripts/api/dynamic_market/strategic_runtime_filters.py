"""Strategic runtime row filtering helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters


JsonRow = dict[str, Any]


def filter_rows_by_analysis_level(
    *,
    rows: Sequence[JsonRow],
    source: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> list[JsonRow]:
    selected = _selected_filters(source=source, analysis_level=analysis_level)
    if not selected:
        return [dict(row) for row in rows]
    filtered: list[JsonRow] = []
    for row in rows:
        dimensions = decode_object(row.get("by_dimension"))
        if all(_row_matches_dimension(dimensions, key, values) for key, values in selected.items()):
            filtered.append(dict(row))
    return filtered


def market_row_for_filtered_rows(market_row: JsonRow, rows: Sequence[JsonRow]) -> JsonRow:
    market_series: dict[str, float] = {}
    for row in rows:
        for period, value in _history_values(row).items():
            market_series[period] = market_series.get(period, 0.0) + value
    filtered = dict(market_row)
    filtered["market_size_series"] = json.dumps(dict(sorted(market_series.items())), ensure_ascii=False)
    filtered["brand_ranking_stacked"] = None
    filtered["company_ranking_stacked"] = None
    filtered["hhi_series_5y"] = None
    return filtered


def decode_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _selected_filters(*, source: str, analysis_level: DynamicMarketAnalysisLevelFilters) -> dict[str, tuple[str, ...]]:
    source_filters = analysis_level.ubist if source == "ubist" else analysis_level.iqvia
    selected: dict[str, tuple[str, ...]] = {}
    for key, values in source_filters.model_dump(by_alias=True).items():
        clean = tuple(str(value).strip() for value in values if str(value).strip())
        if clean:
            selected[key] = clean
    return selected


def _row_matches_dimension(dimensions: Mapping[str, Any], key: str, selected_values: Sequence[str]) -> bool:
    candidates = _dimension_candidates(dimensions, key)
    if not candidates:
        return False
    normalized_selected = {normalize_brand_name(value) or value.strip().lower() for value in selected_values}
    for candidate in candidates:
        text = str(candidate).strip()
        if text in selected_values or (normalize_brand_name(text) or text.lower()) in normalized_selected:
            return True
    return False


def _dimension_candidates(dimensions: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    aliases = {
        "seller": ("seller", "mfr", "manufacturer", "company_name"),
        "class": ("class", "class_name", "market_class"),
        "mfr_name_kor": ("mfr_name_kor", "mfr", "manufacturer", "company_name"),
        "mfr": ("mfr", "mfr_name_kor", "manufacturer", "company_name"),
        "molecule": ("molecule", "molecule_desc"),
        "molecule_strength": ("molecule_strength", "strength_pack", "성분용량"),
        "strength_pack": ("strength_pack", "molecule_strength", "성분용량"),
        "ox_gx": ("ox_gx", "oxgx"),
        "form": ("form", "dosage_form", "제형"),
        "route": ("route", "투여경로"),
        "reimbursement": ("reimbursement", "nhi_type", "nhi", "급여구분"),
        "nhi": ("nhi", "nhi_type", "급여구분"),
        "nhi_type": ("nhi_type", "nhi", "급여구분"),
        "atc3": ("atc3", "atc3_code"),
        "atc4": ("atc4", "atc4_code"),
    }
    values: list[Any] = []
    for alias in aliases.get(key, (key,)):
        value = dimensions.get(alias)
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return tuple(values)


def _history_values(row: Mapping[str, Any]) -> dict[str, float]:
    history = decode_object(row.get("raw_value_history")) or decode_object(row.get("metric_history"))
    values: dict[str, float] = {}
    for period, item in history.items():
        raw = item.get("raw_value", item.get("value", item.get("market_size"))) if isinstance(item, Mapping) else item
        try:
            values[str(period)] = float(raw or 0.0)
        except (TypeError, ValueError):
            values[str(period)] = 0.0
    return values
