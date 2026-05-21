from __future__ import annotations

from typing import Any

from .utils import (
    market_chart_fields,
    market_id_for_brand_row,
    normalise_market_row,
    latest_period,
    parse_json,
)


def _kpi(metric_history: dict[str, Any], extended_history: dict[str, Any]) -> dict[str, Any]:
    period = latest_period(metric_history)
    metric = metric_history.get(period, {}) if period else {}
    extended = extended_history.get(period, {}) if period else {}
    growth_contribution = extended.get("growth_contribution")
    return {
        "latest_period": period,
        "ms": metric.get("ms"),
        "mom": metric.get("mom"),
        "qoq": metric.get("qoq"),
        "yoy": metric.get("yoy"),
        "mat": metric.get("mat"),
        "rank": metric.get("rank"),
        "growth_abs": metric.get("growth_abs"),
        "cagr_1y": extended.get("cagr_1y"),
        "cagr_3y": extended.get("cagr_3y"),
        "cagr_5y": extended.get("cagr_5y"),
        "ei_5y": extended.get("ei_5y"),
        "momentum": extended.get("momentum") or extended.get("momentum_score"),
        "momentum_score": extended.get("momentum_score"),
        "gc": extended.get("gc", growth_contribution),
        "growth_contribution": growth_contribution,
        "growth_contribution_pct": extended.get("growth_contribution_pct"),
        "hhi": extended.get("hhi"),
        "mc5y": extended.get("mc5y", extended.get("market_cagr_5y")),
        "market_cagr_5y": extended.get("market_cagr_5y"),
    }


def build_cause_response_from_rows(
    view_type: str,
    brand_row: dict[str, Any],
    market_row: dict[str, Any] | None,
) -> dict[str, Any]:
    metric_history = parse_json(brand_row.get("metric_history")) or {}
    extended_history = parse_json(brand_row.get("extended_metric_history")) or {}
    raw_value_history = parse_json(brand_row.get("raw_value_history")) or {}
    channel_data = parse_json(brand_row.get("channel_data")) or {}
    specialty_data = parse_json(brand_row.get("specialty_data")) or {}
    by_dimension = parse_json(brand_row.get("by_dimension")) or {}
    overlay_data = parse_json(brand_row.get("overlay_data"))
    cd_overlay = parse_json(brand_row.get("cd_overlay"))
    market_payload = normalise_market_row(view_type, market_row)
    charts = market_chart_fields(market_payload)
    market_id = market_id_for_brand_row(view_type, brand_row)

    response = {
        "brand_name": brand_row.get("brand_name"),
        "brand_key": brand_row.get("brand_key"),
        "is_jw": bool(brand_row.get("is_jw", False)),
        "view": view_type,
        "source": brand_row.get("source"),
        "measure": brand_row.get("measure"),
        "unit_label": brand_row.get("unit_label"),
        "market_id": market_id,
        "market_name": market_payload.get("market_name"),
        "kpi": _kpi(metric_history, extended_history),
        "metric_history": metric_history,
        "extended_metric_history": extended_history,
        "raw_value_history": raw_value_history,
        "channel_data": channel_data,
        "specialty_data": specialty_data,
        "by_dimension": by_dimension,
        "overlay_data": overlay_data,
        "cd_overlay": cd_overlay,
        **charts,
    }
    response["data"] = {
        "kpi": response["kpi"],
        "sources_data": {
            "metric_history": metric_history,
            "extended_metric_history": extended_history,
            "raw_value_history": raw_value_history,
            "channel_data": channel_data,
            "specialty_data": specialty_data,
            "market_size_series": charts["market_size_series"],
            "hhi_series_5y": charts["hhi_series_5y"],
        },
        "ei_ms_matrix": charts["ei_ms_matrix"],
        "growth_contribution": charts["growth_contribution"],
        "growth_contribution_ms_matrix": charts["growth_contribution_ms_matrix"],
        "target_customer_competition": charts["target_customer_competition"],
        "company_concentration_trend": charts["company_concentration_trend"],
        "level_top5_trend": charts["level_top5_trend"],
        "brand_ranking_stacked": charts["brand_ranking_stacked"],
        "company_ranking_stacked": charts["company_ranking_stacked"],
        "analysis_levels": charts["analysis_levels"],
    }
    return response
