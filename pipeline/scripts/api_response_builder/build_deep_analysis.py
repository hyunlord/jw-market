from __future__ import annotations

from typing import Any

from .build_cause import build_cause_response_from_rows
from .utils import latest_period, now_iso


def _series_values(series: dict[str, Any]) -> list[float | None]:
    values: list[float | None] = []
    for value in series.values():
        try:
            values.append(float(value) if value is not None else None)
        except (TypeError, ValueError):
            values.append(None)
    return values


def build_deep_analysis_response_from_rows(
    view_type: str,
    brand_row: dict[str, Any],
    market_row: dict[str, Any] | None,
) -> dict[str, Any]:
    cause = build_cause_response_from_rows(view_type, brand_row, market_row)
    raw_history = cause.get("raw_value_history") or {}
    periods = sorted(raw_history.keys())
    current_period = latest_period(raw_history)
    combo = f"{cause.get('view')}|{cause.get('source')}|{cause.get('measure')}"

    return {
        "brand": cause.get("brand_name"),
        "brand_key": cause.get("brand_key"),
        "market_id": cause.get("market_id"),
        "generated_at": now_iso(),
        "available_combos": [
            {
                "view": cause.get("view"),
                "source": cause.get("source"),
                "measure": cause.get("measure"),
                "unit_label": cause.get("unit_label"),
                "market_id": cause.get("market_id"),
            }
        ],
        "market_meta": {
            "market_name": cause.get("market_name"),
            "by_dimension": cause.get("by_dimension"),
            "target_customer_competition": cause.get("target_customer_competition"),
        },
        "data": {
            "forecast": {
                "by_combo": {
                    combo: {
                        "status": "history_only",
                        "history_periods": periods,
                        "history_values": _series_values(raw_history),
                        "forecast_periods": [],
                        "forecast_values": [],
                        "unit_label": cause.get("unit_label"),
                        "note": "Layer 4 cache preserves mart history; forecasting model is outside this phase.",
                    }
                }
            },
            "simulation": {
                "by_combo": {
                    combo: {
                        "status": "not_computed",
                        "scenarios": [],
                        "note": "Scenario simulation is not recomputed in Phase 16-G-4-Fix-Cache.",
                    }
                }
            },
            "events": {
                "current_period": current_period,
                "items": [],
                "note": "No event catalog was attached to the six-mart cache phase.",
            },
            "ai_analysis": {
                "generated_at": now_iso(),
                "phenomenon": {
                    "title": "Current KPI snapshot",
                    "bullets": [f"Latest period: {cause.get('kpi', {}).get('latest_period')}"],
                },
                "cause": {
                    "title": "Market context",
                    "bullets": ["Market-level chart fields are embedded from the Layer 3 market mart."],
                },
                "prediction": {
                    "title": "Forecast status",
                    "bullets": ["Forecast values are intentionally left empty until a model-backed phase runs."],
                },
                "recommendation": {
                    "title": "Next action",
                    "bullets": ["Use cached cause and market-status payloads for immediate API responses."],
                },
            },
            "cause": cause,
        },
    }
