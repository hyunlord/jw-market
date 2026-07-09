from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Annotated, Any, Final
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
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
ON_DEMAND_FORECAST_WORKERS: Final[int] = 4
ON_DEMAND_LOCK_TIMEOUT_SECONDS: Final[int] = 30
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


@dataclass(frozen=True, slots=True)
class GeneralForecastUnavailable(Exception):
    brand: str
    atc4: str | None
    reason: str
    status_code: int = 404

    def __str__(self) -> str:
        return f"{self.reason}: brand={self.brand!r}, atc4={self.atc4!r}"


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
            SELECT response_json, brand_factors, updated_at
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
                SELECT response_json, updated_at
                FROM cache_deep_analysis
                WHERE brand = %s
                LIMIT 1
                """,
                [brand],
            )
        raise


def _fetch_general_deep_analysis_row(brand: str, atc4: str | None = None) -> dict | None:
    params: list[str] = [brand]
    atc4_clause = ""
    if atc4:
        atc4_clause = "AND atc4_code = %s"
        params.append(atc4)
    try:
        return db.fetch_one(
            f"""
            SELECT response_json, brand_factors, updated_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE brand = %s
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 1
            """,
            params,
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
        raise


def _general_cache_row_from_built(row: Any) -> dict:
    return {
        "response_json": row.response_json,
        "brand_factors": row.brand_factors,
        "updated_at": datetime.now(KST),
        "atc4_code": row.atc4_code,
    }


def _fetch_general_deep_analysis_row_with_conn(conn: Any, brand: str, atc4: str | None = None) -> dict | None:
    params: list[str] = [brand, brand]
    atc4_clause = ""
    if atc4:
        atc4_clause = "AND atc4_code = %s"
        params.append(atc4)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT response_json, brand_factors, updated_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE (brand = %s OR brand_key = %s)
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 1
            """,
            params,
        )
        return cur.fetchone()


def _general_forecast_lock_name(brand: str, atc4: str | None) -> str:
    digest = hashlib.sha256(f"{brand}\0{atc4 or ''}".encode("utf-8")).hexdigest()[:32]
    return f"deep_general:{digest}"


def _acquire_general_forecast_lock(conn: Any, lock_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s) AS acquired", [lock_name, ON_DEMAND_LOCK_TIMEOUT_SECONDS])
        row = cur.fetchone()
    if not isinstance(row, dict):
        return False
    return row.get("acquired") == 1


def _release_general_forecast_lock(conn: Any, lock_name: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
    except pymysql.MySQLError:
        logger.warning("general deep-analysis forecast lock release failed", exc_info=True)


def _build_general_deep_analysis_on_demand(brand: str, atc4: str | None) -> dict:
    """Build and persist one general-view forecast cache row on a cache miss."""

    from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder

    general_builder.apply_api_db_env_fallback()
    conn = general_builder.mariadb_connect()
    lock_name = _general_forecast_lock_name(brand, atc4)
    lock_acquired = False
    try:
        general_builder.assert_d2_database(conn)
        general_builder.ensure_general_cache_table(conn)
        lock_acquired = _acquire_general_forecast_lock(conn, lock_name)
        if not lock_acquired:
            row = _fetch_general_deep_analysis_row_with_conn(conn, brand, atc4)
            if row:
                return row
            raise GeneralForecastUnavailable(
                brand=brand,
                atc4=atc4,
                reason="forecast_generation_in_progress",
                status_code=409,
            )

        row = _fetch_general_deep_analysis_row_with_conn(conn, brand, atc4)
        if row:
            return row

        group_keys = general_builder.select_group_keys(conn, brands={brand}, atc4=atc4, limit_groups=1)
        if not group_keys:
            raise GeneralForecastUnavailable(brand=brand, atc4=atc4, reason="general_market_not_found")

        built = general_builder.build_batch_rows(conn, group_keys, workers=ON_DEMAND_FORECAST_WORKERS, verbose=False)
        if not built:
            raise GeneralForecastUnavailable(brand=brand, atc4=atc4, reason="forecast_empty")

        general_builder.write_rows(conn, built, table_name=general_builder.GENERAL_CACHE_TABLE, batch_size=1)
        return _general_cache_row_from_built(built[0])
    except GeneralForecastUnavailable:
        raise
    except (pymysql.MySQLError, RuntimeError, ValueError) as exc:
        logger.warning("general deep-analysis on-demand forecast failed", exc_info=True)
        raise GeneralForecastUnavailable(brand=brand, atc4=atc4, reason="forecast_generation_failed") from exc
    finally:
        if lock_acquired:
            _release_general_forecast_lock(conn, lock_name)
        conn.close()


def _forecast_unavailable_http_exception(exc: GeneralForecastUnavailable) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "error": "forecast_unavailable",
            "brand": exc.brand,
            "atc4": exc.atc4,
            "reason": exc.reason,
        },
    )


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
        try:
            row = _build_general_deep_analysis_on_demand(brand, atc4)
        except GeneralForecastUnavailable as exc:
            raise _forecast_unavailable_http_exception(exc) from exc
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
        data["brand_strength"] = _load_brand_strength(brand)
    return payload
