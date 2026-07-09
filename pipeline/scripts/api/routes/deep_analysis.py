from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException
import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import get_settings
from pipeline.scripts.api.openapi_docs import DEEP_ANALYSIS_RESPONSES, PORTAL_CORE_TAG


router = APIRouter()
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
FORECAST_HORIZON_QUARTERS = 20
FORECAST_HORIZON_MONTHS = 60
EMPTY_BRAND_FACTORS = {
    "atc": [],
    "ubist": {"seller": [], "molecule_strength": [], "form": [], "route": [], "reimbursement": []},
    "iqvia": {
        "mfr_name_kor": [],
        "molecule_type": [],
        "molecule_desc": [],
        "pack_desc": [],
        "strength": [],
        "nhi_type": [],
    },
}


def _not_generated_brand_strength() -> dict:
    return {"available": False, "reason": "not_generated"}


def _not_generated_ai_variant() -> dict:
    return {"available": False, "reason": "not_generated"}


def _quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _format_agent3_generated_at(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _parse_brand_strength(row: dict) -> dict:
    raw_summary = row.get("strength_summary_json")
    try:
        summary = json.loads(raw_summary)
    except (TypeError, json.JSONDecodeError):
        return _not_generated_brand_strength()
    if not isinstance(summary, dict):
        return _not_generated_brand_strength()
    return {
        "available": True,
        "profile_display": summary.get("profile_display"),
        "strength_items": summary.get("strength_items", []),
        "limitations": summary.get("limitations", []),
        "meta": {
            "generated_at": _format_agent3_generated_at(row.get("generated_at")),
            "workflow_rev": row.get("workflow_rev"),
        },
    }


def _parse_cached_brand_strength(value: object) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict) or payload.get("available") is not True:
        return None
    return payload


def _parse_brand_strength_generated_at(payload: dict | None) -> datetime | None:
    if not payload:
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    raw_value = meta.get("generated_at")
    if raw_value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _load_brand_strength(brand: str, cached_value: object = None) -> dict:
    cached = _parse_cached_brand_strength(cached_value)
    settings = get_settings()
    schema = _quote_identifier(settings.agent3_db_name)
    try:
        row = db.fetch_one(
            f"""
            SELECT strength_summary_json, generated_at, workflow_rev
            FROM {schema}.agent3_brand_strength
            WHERE serving_brand_name = %s
            LIMIT 1
            """,
            [brand],
        )
    except pymysql.MySQLError:
        logger.warning("agent3 brand_strength lookup failed", exc_info=True)
        if cached is not None:
            return cached
        return _not_generated_brand_strength()
    if not row:
        if cached is not None:
            return cached
        return _not_generated_brand_strength()
    live = _parse_brand_strength(row)
    cached_generated_at = _parse_brand_strength_generated_at(cached)
    live_generated_at = _parse_brand_strength_generated_at(live)
    if cached is not None and cached_generated_at is not None and live_generated_at is not None and cached_generated_at >= live_generated_at:
        return cached
    return live


def _load_ai_analysis(brand: str) -> dict:
    try:
        row = db.fetch_one(
            """
            SELECT ai_analysis_json
            FROM cache_deep_analysis_ai_analysis
            WHERE brand = %s
            LIMIT 1
            """,
            [brand],
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] == 1146:
            return {}
        raise
    if not row or not row.get("ai_analysis_json"):
        return {}
    try:
        payload = json.loads(row["ai_analysis_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_ai_variant(value: object) -> dict:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return _not_generated_ai_variant()
    if not isinstance(payload, dict):
        return _not_generated_ai_variant()
    return _normalize_ai_variant_payload(payload)


def _normalize_ai_variant_payload(payload: dict) -> dict:
    normalized = dict(payload)
    normalized.pop("analysis_variant", None)
    evidence_pool = normalized.get("evidence_pool")
    if isinstance(evidence_pool, list):
        normalized["evidence_pool"] = [
            {key: item_value for key, item_value in item.items() if key != "published_date"}
            if isinstance(item, dict)
            else item
            for item in evidence_pool
        ]
    return normalized


def _load_ai_analysis_variants(brand: str) -> tuple[dict, dict]:
    try:
        row = db.fetch_one(
            """
            SELECT ai_analysis_short_json, ai_analysis_long_json
            FROM cache_deep_analysis_ai_analysis
            WHERE brand = %s
            LIMIT 1
            """,
            [brand],
        )
    except pymysql.MySQLError:
        logger.warning("AI analysis variant lookup failed", exc_info=True)
        return _not_generated_ai_variant(), _not_generated_ai_variant()
    if not row:
        return _not_generated_ai_variant(), _not_generated_ai_variant()
    return (
        _parse_ai_variant(row.get("ai_analysis_short_json")),
        _parse_ai_variant(row.get("ai_analysis_long_json")),
    )


def _empty_brand_factors() -> dict:
    return json.loads(json.dumps(EMPTY_BRAND_FACTORS))


def _load_brand_factors(value: object) -> dict:
    if value is None:
        return _empty_brand_factors()
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return _empty_brand_factors()
    return payload if isinstance(payload, dict) else _empty_brand_factors()


def _fetch_deep_analysis_row(brand: str) -> dict | None:
    try:
        return db.fetch_one(
            """
            SELECT response_json, brand_factors, brand_strength, updated_at
            FROM cache_deep_analysis
            WHERE brand = %s
            LIMIT 1
            """,
            [brand],
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] == 1054:
            return db.fetch_one(
                """
                SELECT response_json, brand_factors, updated_at
                FROM cache_deep_analysis
                WHERE brand = %s
                LIMIT 1
                """,
                [brand],
            )
        raise


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
    for key, value in forecast_intervals.items():
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
    forecast_ms_pct = combo.get("forecast_ms_pct")
    if isinstance(forecast_ms_pct, list):
        combo["forecast_ms_pct"] = forecast_ms_pct[:horizon]
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
        forecast_ms_pct = brand.get("forecast_ms_pct")
        if isinstance(forecast_ms_pct, list):
            brand["forecast_ms_pct"] = forecast_ms_pct[:horizon]
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


@router.get(
    "/api/deep-analysis/{brand_name}",
    tags=[PORTAL_CORE_TAG],
    summary="포탈 심층분석 조회",
    description=(
        "cache_deep_analysis와 ai_analysis 보조 cache를 합쳐 포탈 심층분석 payload를 반환합니다. "
        "월/분기 forecast horizon은 화면 계약에 맞게 잘라서 제공합니다."
    ),
    response_model=None,
    responses=DEEP_ANALYSIS_RESPONSES,
)
def deep_analysis(brand_name: str) -> dict:
    brand = unquote(brand_name)
    row = _fetch_deep_analysis_row(brand)
    if not row:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_deep_analysis"})
    payload["generated_at"] = _format_generated_at(row.get("updated_at"))
    _slice_forecast_horizon(payload)
    data = payload.setdefault("data", {})
    if isinstance(data, dict):
        if "brand_factors" in row:
            data["brand_factors"] = _load_brand_factors(row.get("brand_factors"))
        data["ai_analysis"] = _load_ai_analysis(brand)
        ai_analysis_short, ai_analysis_long = _load_ai_analysis_variants(brand)
        data["ai_analysis_short"] = ai_analysis_short
        data["ai_analysis_long"] = ai_analysis_long
        data["brand_strength"] = _load_brand_strength(brand, row.get("brand_strength"))
    return payload
