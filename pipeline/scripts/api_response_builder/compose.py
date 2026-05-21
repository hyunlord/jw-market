from __future__ import annotations

from typing import Any


MARKET_CHART_KEYS = [
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "company_concentration_trend",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "level_top5_trend",
    "target_customer_competition",
]


def extract_market_charts_from_market_status(market_response: dict[str, Any] | None) -> dict[str, Any]:
    """Return the market-level chart payload that cause responses join at serve time."""
    if not market_response:
        return {}

    source = market_response.get("data")
    if not isinstance(source, dict):
        source = market_response

    return {key: source[key] for key in MARKET_CHART_KEYS if key in source}


def compose_cause_response(
    cause_response: dict[str, Any],
    market_response: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Compose compact cause cache with market-status cache.

    Phase 16-G-4-Fix-CacheSize stores cause rows as brand-specific data plus a
    market cache reference. The served API response keeps the same compact
    top-level metadata while injecting market charts into data.*.
    """
    data = dict(cause_response.get("data") or {})
    data.update(extract_market_charts_from_market_status(market_response))

    return {
        "brand_name": cause_response.get("brand_name"),
        "brand_key": cause_response.get("brand_key"),
        "is_jw": cause_response.get("is_jw"),
        "view": cause_response.get("view"),
        "source": cause_response.get("source"),
        "measure": cause_response.get("measure"),
        "unit_label": cause_response.get("unit_label"),
        "market_id": cause_response.get("market_id"),
        "market_name": cause_response.get("market_name"),
        "data": data,
    }
