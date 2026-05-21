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
        "market_id",
        "market_name",
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
        for key in ["kpi", "sources_data", "by_dimension"]:
            if key not in response["data"]:
                missing.append(f"data.{key}")
        sources_data = response["data"].get("sources_data")
        if isinstance(sources_data, dict):
            for key in [
                "metric_history",
                "extended_metric_history",
                "raw_value_history",
                "channel_data",
                "specialty_data",
                "market_size_series",
                "hhi_series_5y",
            ]:
                if key not in sources_data:
                    missing.append(f"data.sources_data.{key}")
        else:
            missing.append("data.sources_data")
    if endpoint == "deep-analysis" and isinstance(response.get("data"), dict):
        for key in ["forecast", "simulation", "events", "ai_analysis"]:
            if key not in response["data"]:
                missing.append(f"data.{key}")
    return missing
