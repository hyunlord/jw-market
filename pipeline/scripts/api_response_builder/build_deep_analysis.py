from __future__ import annotations

from typing import Any

from .utils import (
    latest_period,
    market_id_for_brand_row,
    normalise_market_row,
    now_iso,
    parse_json,
)


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
    raw_history = parse_json(brand_row.get("raw_value_history")) or {}
    periods = sorted(raw_history.keys())
    current_period = latest_period(raw_history)
    source = brand_row.get("source")
    measure = brand_row.get("measure")
    unit_label = brand_row.get("unit_label")
    market_id = market_id_for_brand_row(view_type, brand_row)
    market_payload = normalise_market_row(view_type, market_row)
    by_dimension = parse_json(brand_row.get("by_dimension")) or {}
    combo = f"{view_type}|{source}|{measure}"

    return {
        "brand": brand_row.get("brand_name"),
        "brand_key": brand_row.get("brand_key"),
        "market_id": market_id,
        "view": view_type,
        "source": source,
        "measure": measure,
        "generated_at": now_iso(),
        "available_combos": [
            {
                "view": view_type,
                "source": source,
                "measure": measure,
                "unit_label": unit_label,
                "market_id": market_id,
            }
        ],
        "market_meta": {
            "market_name": market_payload.get("market_name"),
            "market_id": market_id,
            "by_dimension": by_dimension,
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
                        "unit_label": unit_label,
                        "note": "History-only shell; forecasting model is outside this phase.",
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
                    "bullets": [f"Latest period: {current_period}"],
                },
                "cause": {
                    "title": "Market context",
                    "bullets": ["Use cause and market-status cache payloads for chart-level context."],
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
        },
    }
