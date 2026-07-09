from __future__ import annotations

from typing import Annotated
from datetime import datetime, timedelta, timezone
import json
import logging
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import get_settings
from pipeline.scripts.api.openapi_docs import DEEP_ANALYSIS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


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


def _compact_sql(column: str) -> str:
    return f"REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '')"


def _single_compact_row(rows: list[dict]) -> dict | None:
    brands = {str(row.get("brand") or row.get("serving_brand_name") or "") for row in rows}
    if len(brands) != 1:
        return None
    return rows[0]


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


def _load_brand_strength(brand: str) -> dict:
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
        if not row:
            compact = compact_brand_name(brand)
            if compact and compact != brand:
                rows = db.fetch_all(
                    f"""
                    SELECT serving_brand_name, strength_summary_json, generated_at, workflow_rev
                    FROM {schema}.agent3_brand_strength
                    WHERE {_compact_sql("serving_brand_name")} = %s
                    LIMIT 2
                    """,
                    [compact],
                )
                row = _single_compact_row(rows)
    except pymysql.MySQLError:
        logger.warning("agent3 brand_strength lookup failed", exc_info=True)
        return _not_generated_brand_strength()
    if not row:
        return _not_generated_brand_strength()
    return _parse_brand_strength(row)


def _load_ai_analysis(brand: str) -> dict:
    try:
        row = db.fetch_one(
            """
            SELECT brand, ai_analysis_json
            FROM cache_deep_analysis_ai_analysis
            WHERE brand = %s
            LIMIT 1
            """,
            [brand],
        )
        if not row:
            compact = compact_brand_name(brand)
            if compact and compact != brand:
                rows = db.fetch_all(
                    f"""
                    SELECT brand, ai_analysis_json
                    FROM cache_deep_analysis_ai_analysis
                    WHERE {_compact_sql("brand")} = %s
                    LIMIT 2
                    """,
                    [compact],
                )
                row = _single_compact_row(rows)
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
            SELECT brand, ai_analysis_short_json, ai_analysis_long_json
            FROM cache_deep_analysis_ai_analysis
            WHERE brand = %s
            LIMIT 1
            """,
            [brand],
        )
        if not row:
            compact = compact_brand_name(brand)
            if compact and compact != brand:
                rows = db.fetch_all(
                    f"""
                    SELECT brand, ai_analysis_short_json, ai_analysis_long_json
                    FROM cache_deep_analysis_ai_analysis
                    WHERE {_compact_sql("brand")} = %s
                    LIMIT 2
                    """,
                    [compact],
                )
                row = _single_compact_row(rows)
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
        row = db.fetch_one(
            """
            SELECT brand, response_json, brand_factors, updated_at
            FROM cache_deep_analysis
            WHERE brand = %s
            LIMIT 1
            """,
            [brand],
        )
        if row:
            return row
        compact = compact_brand_name(brand)
        if not compact or compact == brand:
            return None
        rows = db.fetch_all(
            f"""
            SELECT brand, response_json, brand_factors, updated_at
            FROM cache_deep_analysis
            WHERE {_compact_sql("brand")} = %s
            LIMIT 2
            """,
            [compact],
        )
        return _single_compact_row(rows)
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] == 1054:
            row = db.fetch_one(
                """
                SELECT brand, response_json, updated_at
                FROM cache_deep_analysis
                WHERE brand = %s
                LIMIT 1
                """,
                [brand],
            )
            if row:
                return row
            compact = compact_brand_name(brand)
            if not compact or compact == brand:
                return None
            rows = db.fetch_all(
                f"""
                SELECT brand, response_json, updated_at
                FROM cache_deep_analysis
                WHERE {_compact_sql("brand")} = %s
                LIMIT 2
                """,
                [compact],
            )
            return _single_compact_row(rows)
        raise


def _fetch_general_deep_analysis_row(brand: str, atc4: str | None = None) -> dict | None:
    params: list[str] = [brand, brand]
    atc4_clause = ""
    if atc4:
        atc4_clause = "AND atc4_code = %s"
        params.append(atc4)
    try:
        row = db.fetch_one(
            f"""
            SELECT brand_key, brand, response_json, brand_factors, updated_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE (brand = %s OR brand_key = %s)
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 1
            """,
            params,
        )
        if row:
            return row
        compact = compact_brand_name(brand)
        if not compact or compact == brand:
            return None
        compact_params: list[str] = [compact, compact]
        if atc4:
            compact_params.append(atc4)
        rows = db.fetch_all(
            f"""
            SELECT brand_key, brand, response_json, brand_factors, updated_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE ({_compact_sql("brand")} = %s OR brand_key = %s)
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 2
            """,
            compact_params,
        )
        return _single_compact_row(rows)
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
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
def deep_analysis(
    brand_name: str,
    atc4: Annotated[
        str | None,
        Query(description="일반뷰 deep-analysis 캐시에서 특정 ATC4 시장을 지정합니다."),
    ] = None,
) -> dict:
    brand = unquote(brand_name)
    row = _fetch_general_deep_analysis_row(brand, atc4) if atc4 else _fetch_deep_analysis_row(brand)
    if not row and not atc4:
        row = _fetch_general_deep_analysis_row(brand)
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
        matched_brand = str(row.get("brand") or row.get("brand_key") or brand)
        data["ai_analysis"] = _load_ai_analysis(matched_brand)
        ai_analysis_short, ai_analysis_long = _load_ai_analysis_variants(matched_brand)
        data["ai_analysis_short"] = ai_analysis_short
        data["ai_analysis_long"] = ai_analysis_long
        data["brand_strength"] = _load_brand_strength(matched_brand)
    return payload
