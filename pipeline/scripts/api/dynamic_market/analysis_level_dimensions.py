"""Dimension-row adapters for dynamic analysis-level sections."""

from __future__ import annotations

import json
import logging
from typing import Any

from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.analysis_level_series import (
    metric_history_from_periods,
    with_dimension_series_from_labels,
)
from pipeline.scripts.api.dynamic_market.aggregator import (
    brand_matrix_summary_scope,
    merge_json_object,
)
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, BrandRef, MarketDefinition, quote_identifier


logger = logging.getLogger(__name__)

IQVIA_GENERAL_DIMENSIONS = frozenset({"mfr", "molecule_type", "molecule_desc", "strength", "nhi"})
UBIST_GENERAL_DIMENSIONS = frozenset({"seller", "molecule", "molecule_strength", "form", "route", "reimbursement"})
GENERAL_SIDECAR_DIMENSIONS_BY_SOURCE = {
    "iqvia_nsa": IQVIA_GENERAL_DIMENSIONS,
    "ubist": UBIST_GENERAL_DIMENSIONS,
}


def build_analysis_rows(
    *,
    definition: MarketDefinition,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    mart_db: str,
    reuse_general_dimensions: bool = False,
) -> list[dict[str, Any]]:
    """Return cache-cause mart rows enriched for analysis-level builders."""

    general_dimensions = (
        _general_dimensions_from_metrics(metrics)
        if reuse_general_dimensions
        else _general_dimensions_by_pair(metrics=metrics, mart_db=mart_db)
    )
    sidecar_dimensions = _general_sidecar_dimensions_by_pair(metrics=metrics, mart_db=mart_db)
    strategic_dimensions = _strategic_dimensions_by_brand(
        definition=definition,
        source=metrics.source,
        measure=metrics.measure,
        mart_db=mart_db,
    )
    rows = _analysis_rows(
        metrics=metrics,
        focus=focus,
        general_dimensions=general_dimensions,
        sidecar_dimensions=sidecar_dimensions,
        strategic_dimensions=strategic_dimensions,
    )
    return rows


def _general_dimensions_from_metrics(metrics: AggregatedMetrics) -> dict[tuple[str, str], dict[str, Any]]:
    """Reuse general dimension fields already loaded by the request aggregator."""

    dimensions: dict[tuple[str, str], dict[str, Any]] = {}
    for brand in metrics.all_brands:
        dimensions[(brand.brand_key, brand.atc4_code)] = {
            key: brand.analysis_row.get(key)
            for key in (
                "by_dimension",
                "dimension_data",
                "dimension_channel_data",
                "channel_data",
                "channel_specialty_matrix",
            )
        }
    return dimensions


def _general_dimensions_by_pair(
    *,
    metrics: AggregatedMetrics,
    mart_db: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    if not metrics.all_brands:
        return {}
    scope_sql, scope_params, pair_scope = brand_matrix_summary_scope(_brand_refs(metrics))
    rows = db.fetch_all(
        f"""
        SELECT brand_key, brand_name, atc4_code, source, measure, unit_label,
               by_dimension, dimension_data, dimension_channel_data, channel_data,
               channel_specialty_matrix
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric
        WHERE source = %s
          AND measure = %s
          AND {scope_sql}
        ORDER BY brand_name, brand_key
        """,
        (metrics.source, metrics.measure, *scope_params),
    )
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    filtered_rows = 0
    for row in rows:
        key = (str(row["brand_key"]), str(row["atc4_code"]))
        if pair_scope and key not in pair_scope:
            filtered_rows += 1
            continue
        payloads[key] = {
            "by_dimension": row.get("by_dimension"),
            "dimension_data": row.get("dimension_data"),
            "dimension_channel_data": row.get("dimension_channel_data"),
            "channel_data": row.get("channel_data"),
            "channel_specialty_matrix": row.get("channel_specialty_matrix"),
        }
    logger.debug("dynamic_analysis_level_general_pair_filter filtered_rows=%s", filtered_rows)
    return payloads


def _general_sidecar_dimensions_by_pair(
    *,
    metrics: AggregatedMetrics,
    mart_db: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    dimension_types = GENERAL_SIDECAR_DIMENSIONS_BY_SOURCE.get(metrics.source)
    if not dimension_types or not metrics.all_brands:
        return {}
    scope_sql, scope_params, pair_scope = brand_matrix_summary_scope(_brand_refs(metrics))
    rows = db.fetch_all(
        f"""
        SELECT brand_key, brand_name, atc4_code, dimension_type, dimension_value, raw_value_history
        FROM {quote_identifier(mart_db)}.mart_general_filter_dimension_metric
        WHERE source = %s
          AND measure = %s
          AND dimension_type IN %s
          AND {scope_sql}
        ORDER BY brand_name, brand_key, dimension_type, dimension_value
        """,
        (metrics.source, metrics.measure, tuple(sorted(dimension_types)), *scope_params),
    )
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["brand_key"]), str(row["atc4_code"]))
        if pair_scope and key not in pair_scope:
            continue
        dimension_type = str(row.get("dimension_type") or "")
        dimension_value = str(row.get("dimension_value") or "").strip()
        if dimension_type not in dimension_types or not dimension_value:
            continue
        payload = payloads.setdefault(key, {"by_dimension": {}, "dimension_data": {}})
        payload["by_dimension"].setdefault(dimension_type, dimension_value)
        field_data = payload["dimension_data"].setdefault(dimension_type, {})
        field_data.setdefault(dimension_value, {})
        _add_history(field_data[dimension_value], row.get("raw_value_history"))
    return {
        key: {
            "by_dimension": json.dumps(value["by_dimension"], ensure_ascii=False, sort_keys=True),
            "dimension_data": json.dumps(value["dimension_data"], ensure_ascii=False, sort_keys=True),
        }
        for key, value in payloads.items()
    }


def _strategic_dimensions_by_brand(
    *,
    definition: MarketDefinition,
    source: str,
    measure: str,
    mart_db: str,
) -> dict[str, dict[str, Any]]:
    market = definition.market_catalog_row or {}
    ml_id = market.get("ml_id")
    if not ml_id:
        return {}
    rows = db.fetch_all(
        f"""
        SELECT brand_key, brand_name, by_dimension, is_jw
        FROM {quote_identifier(mart_db)}.mart_strategic_ml_brand_metric
        WHERE ml_id = %s
          AND source = %s
          AND measure = %s
        ORDER BY brand_name, brand_key
        """,
        (str(ml_id), source, measure),
    )
    dimensions: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = {
            "by_dimension": row.get("by_dimension"),
            "is_jw": bool(row.get("is_jw")),
        }
        brand_key = row.get("brand_key")
        brand_name = row.get("brand_name")
        if brand_key:
            dimensions[str(brand_key)] = value
        if brand_name:
            dimensions[str(brand_name)] = value
    return dimensions


def _analysis_rows(
    *,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    general_dimensions: dict[tuple[str, str], dict[str, Any]],
    sidecar_dimensions: dict[tuple[str, str], dict[str, Any]],
    strategic_dimensions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    totals_by_period = {
        str(item["period"]): float(item.get("market_size") or 0.0)
        for item in metrics.monthly_series
    }
    rows: list[dict[str, Any]] = []
    for brand in metrics.all_brands:
        row = _base_analysis_row(brand=brand, metrics=metrics, focus=focus)
        key = (brand.brand_key, brand.atc4_code)
        row = _merge_dimension_payload(row, general_dimensions.get(key))
        row = _merge_dimension_payload(row, sidecar_dimensions.get(key))
        strategic = strategic_dimensions.get(brand.brand_key) or strategic_dimensions.get(brand.brand_name) or {}
        if strategic.get("by_dimension"):
            row["by_dimension"] = _merge_dimension_labels_preserving(
                row.get("by_dimension"),
                strategic["by_dimension"],
                protected={"molecule"},
            )
        row["is_jw"] = bool(row["is_target"] or strategic.get("is_jw"))
        row["metric_history"] = _metric_history(brand=brand, totals_by_period=totals_by_period)
        row["dimension_data"] = with_dimension_series_from_labels(
            row.get("dimension_data"),
            row.get("by_dimension"),
            brand.history_by_period,
        )
        rows.append(row)
    return rows


def _base_analysis_row(
    *,
    brand: BrandMetric,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
) -> dict[str, Any]:
    row = dict(brand.analysis_row)
    row["brand_key"] = brand.brand_key
    row["brand_name"] = brand.brand_name
    row["atc4_code"] = brand.atc4_code
    row["source"] = metrics.source
    row["measure"] = metrics.measure
    row["unit_label"] = metrics.unit_label
    row["is_target"] = bool(focus and brand.brand_key == focus.brand_key)
    return row


def _metric_history(
    *,
    brand: BrandMetric,
    totals_by_period: dict[str, float],
) -> dict[str, dict[str, float | int]]:
    return metric_history_from_periods(
        history_by_period=brand.history_by_period,
        totals_by_period=totals_by_period,
        rank=brand.rank,
    )


def _merge_dimension_payload(row: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return row
    merged = dict(row)
    for key in ("by_dimension", "dimension_data", "dimension_channel_data", "channel_data", "channel_specialty_matrix"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        if key == "by_dimension":
            merged[key] = _merge_dimension_labels(merged.get(key), value)
        elif key in ("dimension_data", "dimension_channel_data"):
            merged[key] = merge_json_object(merged.get(key), value)
        else:
            merged[key] = value
    return merged


def _merge_dimension_labels(existing: Any, extra: Any) -> str:
    merged = _json_object(existing)
    for key, value in _json_object(extra).items():
        if _is_empty_dimension_value(value):
            continue
        merged[key] = value
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def _merge_dimension_labels_preserving(existing: Any, extra: Any, *, protected: set[str]) -> str:
    merged = _json_object(existing)
    for key, value in _json_object(extra).items():
        if (key in protected and key in merged) or _is_empty_dimension_value(value):
            continue
        merged[key] = value
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def _add_history(target: dict[str, Any], raw_history: Any) -> None:
    history = _json_object(raw_history)
    for period, item in history.items():
        bucket = target.setdefault(str(period), {"raw_value": 0.0})
        if not isinstance(bucket, dict):
            continue
        if isinstance(item, dict):
            value = item.get("raw_value") or item.get("value") or 0.0
        else:
            value = item or 0.0
        try:
            bucket["raw_value"] = float(bucket.get("raw_value") or 0.0) + float(value or 0.0)
        except (TypeError, ValueError):
            continue


def _is_empty_dimension_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == "null"


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        payload = json.loads(raw)
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _brand_refs(metrics: AggregatedMetrics) -> tuple[BrandRef, ...]:
    return tuple(BrandRef(item.brand_key, item.brand_name, item.atc4_code) for item in metrics.all_brands)
