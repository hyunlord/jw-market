from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json


router = APIRouter()

KST = timezone(timedelta(hours=9))
FORECAST_HORIZON_QUARTERS = 4
FORECAST_HORIZON_MONTHS = 12
FORECAST_INTERVAL_KEYS = (
    "upper_horizon_adaptive",
    "lower_horizon_adaptive",
    "upper_95_natural",
    "lower_95_natural",
    "ci_upper_95",
    "ci_lower_95",
)


def _load_ai_analysis(brand: str) -> dict:
    row = db.fetch_one(
        """
        SELECT ai_analysis_json
        FROM cache_deep_analysis_ai_analysis
        WHERE brand = %s
        LIMIT 1
        """,
        [brand],
    )
    if not row or not row.get("ai_analysis_json"):
        return {}
    try:
        payload = json.loads(row["ai_analysis_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _format_generated_at(value: object) -> str:
    if isinstance(value, datetime):
        generated_at = value
    else:
        try:
            generated_at = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            generated_at = datetime.now(KST)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=KST)
    else:
        generated_at = generated_at.astimezone(KST)
    return generated_at.isoformat(timespec="seconds")


def _forecast_horizon_for_combo(combo: dict) -> int | None:
    period_unit = combo.get("period_unit")
    if period_unit == "분기":
        return FORECAST_HORIZON_QUARTERS
    if period_unit == "월":
        return FORECAST_HORIZON_MONTHS
    return None


def _slice_forecast_intervals(forecast_intervals: object, horizon: int) -> None:
    if not isinstance(forecast_intervals, dict):
        return
    for key in FORECAST_INTERVAL_KEYS:
        value = forecast_intervals.get(key)
        if isinstance(value, list):
            forecast_intervals[key] = value[:horizon]


def _slice_forecast_combo(combo: object) -> None:
    if not isinstance(combo, dict):
        return
    horizon = _forecast_horizon_for_combo(combo)
    if horizon is None:
        return

    forecast_periods = combo.get("forecast_periods")
    if isinstance(forecast_periods, list):
        combo["forecast_periods"] = forecast_periods[:horizon]

    forecast_values = combo.get("forecast_values")
    if isinstance(forecast_values, list):
        combo["forecast_values"] = forecast_values[:horizon]
    _slice_forecast_intervals(combo.get("forecast_intervals"), horizon)

    brands = combo.get("brands")
    if not isinstance(brands, list):
        return
    for brand in brands:
        if not isinstance(brand, dict):
            continue
        forecast_values = brand.get("forecast_values")
        if isinstance(forecast_values, list):
            brand["forecast_values"] = forecast_values[:horizon]
        _slice_forecast_intervals(brand.get("forecast_intervals"), horizon)


def _slice_forecast_horizon(payload: dict) -> None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    forecast = data.get("forecast")
    if not isinstance(forecast, dict):
        return
    by_combo = forecast.get("by_combo")
    if not isinstance(by_combo, dict):
        return
    for combo in by_combo.values():
        _slice_forecast_combo(combo)


@router.get("/api/deep-analysis/{brand_name}")
def deep_analysis(brand_name: str) -> dict:
    brand = unquote(brand_name)
    row = db.fetch_one(
        """
        SELECT response_json, updated_at
        FROM cache_deep_analysis
        WHERE brand = %s
        LIMIT 1
        """,
        [brand],
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_deep_analysis"})
    payload["generated_at"] = _format_generated_at(row.get("updated_at"))
    _slice_forecast_horizon(payload)
    data = payload.setdefault("data", {})
    if isinstance(data, dict):
        data["ai_analysis"] = _load_ai_analysis(brand)
    return payload
