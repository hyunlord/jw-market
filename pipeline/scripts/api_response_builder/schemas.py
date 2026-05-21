from __future__ import annotations

from typing import Any


REQUIRED_KEYS = {
    "brands": ["brands", "total_count", "filters_applied"],
    "market-status": [
        "market_id",
        "view",
        "source",
        "measure",
        "market_size_series",
        "brand_ranking_stacked",
        "target_customer_competition",
    ],
    "cause": [
        "brand_name",
        "brand_key",
        "view",
        "source",
        "measure",
        "kpi",
        "metric_history",
        "channel_data",
        "specialty_data",
        "market_size_series",
        "target_customer_competition",
        "data",
    ],
    "deep-analysis": [
        "brand",
        "brand_key",
        "market_id",
        "generated_at",
        "available_combos",
        "market_meta",
        "data",
    ],
}


def validate_response(endpoint: str, response: dict[str, Any]) -> list[str]:
    missing = [key for key in REQUIRED_KEYS[endpoint] if key not in response]
    if endpoint == "cause" and isinstance(response.get("data"), dict):
        for key in [
            "ei_ms_matrix",
            "growth_contribution",
            "sources_data",
            "growth_contribution_ms_matrix",
            "target_customer_competition",
            "company_concentration_trend",
            "level_top5_trend",
            "brand_ranking_stacked",
            "company_ranking_stacked",
            "analysis_levels",
            "kpi",
        ]:
            if key not in response["data"]:
                missing.append(f"data.{key}")
    if endpoint == "deep-analysis" and isinstance(response.get("data"), dict):
        for key in ["forecast", "simulation", "events", "ai_analysis"]:
            if key not in response["data"]:
                missing.append(f"data.{key}")
    return missing
