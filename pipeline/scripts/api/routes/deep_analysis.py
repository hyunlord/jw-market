from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from typing import Annotated, Any, Final, Literal
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandSetInputError,
    BrandSetResolutionError,
    resolve_brand_set,
)
from pipeline.scripts.api.deep_analysis_brand_elements import (
    build_brand_factors,
    fallback_brand_choices,
)
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import get_settings
from pipeline.scripts.api.openapi_docs import DEEP_ANALYSIS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


router = APIRouter()
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
FORECAST_HORIZON_QUARTERS = 20
FORECAST_HORIZON_MONTHS = 60
ON_DEMAND_FORECAST_WORKERS: Final[int] = 4
ON_DEMAND_LOCK_TIMEOUT_SECONDS: Final[int] = 30
GENERAL_CACHE_TTL_DAYS: Final[int] = 35
BRAND_ELEMENTS_CACHE_TTL_DAYS: Final[int] = 35
SOURCE_BRAND_STRENGTH_TABLE: Final[str] = "agent3_brand_strength_source"
SOURCE_BRAND_STRENGTH_SOURCES: Final[tuple[str, str]] = ("iqvia", "ubist")
BRAND_FACTOR_DB_SOURCES: Final[dict[str, str]] = {"iqvia": "iqvia_nsa", "ubist": "ubist"}
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


@dataclass(frozen=True, slots=True)
class CompactBrandLookupAmbiguous(Exception):
    brand: str

    def __str__(self) -> str:
        return f"ambiguous compact brand lookup: {self.brand!r}"


def _not_generated_brand_strength() -> dict:
    return {"available": False, "reason": "not_generated"}


def _not_generated_ai_variant() -> dict:
    return {"available": False, "reason": "not_generated"}


def _quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _ttl_days(env_name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env_name, str(default))))
    except ValueError:
        return default


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=KST)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def _row_cache_fresh(row: dict, *, ttl_days: int) -> bool:
    now = datetime.now(KST)
    expires_at = _coerce_datetime(row.get("expires_at"))
    if expires_at is not None:
        return expires_at > now
    updated_at = _coerce_datetime(row.get("updated_at"))
    if updated_at is None:
        return True
    return updated_at + timedelta(days=ttl_days) > now


def _row_marked_stale(row: dict) -> bool:
    value = row.get("is_stale")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def _compact_sql(column: str) -> str:
    return f"REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '')"


def _single_compact_row(rows: list[dict], *, raise_on_ambiguous: bool = False, brand: str = "") -> dict | None:
    brands = {str(row.get("brand") or row.get("serving_brand_name") or "") for row in rows}
    if len(brands) != 1:
        if raise_on_ambiguous and len(brands) > 1:
            raise CompactBrandLookupAmbiguous(brand=brand)
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


def _parse_source_brand_strength(row: dict) -> dict:
    raw_summary = row.get("strength_summary_json")
    try:
        summary = json.loads(raw_summary)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(summary, dict):
        return {}
    strength_items = summary.get("strength_items", [])
    limitations = summary.get("limitations", [])
    return {
        "profile_display": summary.get("profile_display") or {},
        "strength_items": strength_items if isinstance(strength_items, list) else [],
        "limitations": limitations if isinstance(limitations, list) else [],
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


def _source_strength_exact_rows(schema: str, brand_keys: list[str]) -> list[dict]:
    placeholders = ", ".join(["%s"] * len(brand_keys))
    source_placeholders = ", ".join(["%s"] * len(SOURCE_BRAND_STRENGTH_SOURCES))
    return db.fetch_all(
        f"""
        SELECT brand_key, serving_brand_name, source, strength_summary_json
        FROM {schema}.{_quote_identifier(SOURCE_BRAND_STRENGTH_TABLE)}
        WHERE brand_key IN ({placeholders})
          AND source IN ({source_placeholders})
        """,
        [*brand_keys, *SOURCE_BRAND_STRENGTH_SOURCES],
    )


def _source_strength_compact_rows(schema: str, compact_values: list[str]) -> list[dict]:
    compact_placeholders = ", ".join(["%s"] * len(compact_values))
    source_placeholders = ", ".join(["%s"] * len(SOURCE_BRAND_STRENGTH_SOURCES))
    return db.fetch_all(
        f"""
        SELECT brand_key, serving_brand_name, source, strength_summary_json
        FROM {schema}.{_quote_identifier(SOURCE_BRAND_STRENGTH_TABLE)}
        WHERE {_compact_sql("serving_brand_name")} IN ({compact_placeholders})
          AND source IN ({source_placeholders})
        """,
        [*compact_values, *SOURCE_BRAND_STRENGTH_SOURCES],
    )


def _load_brand_strength_by_source(brand_keys: list[str]) -> dict[str, dict[str, dict]]:
    keys = [key for key in dict.fromkeys(str(value) for value in brand_keys if str(value).strip())]
    if not keys:
        return {}

    settings = get_settings()
    schema = _quote_identifier(settings.db_name)
    result: dict[str, dict[str, dict]] = {key: {} for key in keys}
    try:
        for row in _source_strength_exact_rows(schema, keys):
            brand_key = str(row.get("brand_key") or "")
            source = str(row.get("source") or "")
            if brand_key in result and source in SOURCE_BRAND_STRENGTH_SOURCES:
                parsed = _parse_source_brand_strength(row)
                if parsed:
                    result[brand_key][source] = parsed

        missing_by_compact: dict[str, list[str]] = {}
        for key in keys:
            missing_sources = [source for source in SOURCE_BRAND_STRENGTH_SOURCES if source not in result[key]]
            if missing_sources:
                compact = compact_brand_name(key)
                if compact:
                    missing_by_compact.setdefault(compact, []).append(key)
        if missing_by_compact:
            rows_by_lookup: dict[tuple[str, str], list[dict]] = {}
            for row in _source_strength_compact_rows(schema, list(missing_by_compact)):
                compact = compact_brand_name(str(row.get("serving_brand_name") or row.get("brand_key") or ""))
                source = str(row.get("source") or "")
                if compact and source in SOURCE_BRAND_STRENGTH_SOURCES:
                    rows_by_lookup.setdefault((compact, source), []).append(row)

            for (compact, source), rows in rows_by_lookup.items():
                distinct_brands = {str(row.get("serving_brand_name") or row.get("brand_key") or "") for row in rows}
                if len(distinct_brands) != 1:
                    logger.warning(
                        "ambiguous source brand_strength compact lookup skipped",
                        extra={"compact_brand": compact, "source": source, "matched_brands": sorted(distinct_brands)},
                    )
                    continue
                parsed = _parse_source_brand_strength(rows[0])
                if not parsed:
                    continue
                for target in missing_by_compact.get(compact, []):
                    result[target].setdefault(source, parsed)
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return {}
        raise
    except pymysql.MySQLError:
        logger.warning("agent3 source brand_strength lookup failed", exc_info=True)
        return {}

    return {key: value for key, value in result.items() if value}


def _parse_cached_brand_element(row: dict) -> dict:
    def parse_json_field(name: str) -> dict:
        try:
            payload = json.loads(row.get(name) or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    return {
        "brand": str(row.get("brand_name") or row.get("brand_key") or ""),
        "brand_key": str(row.get("brand_key") or row.get("brand_name") or ""),
        "factors": parse_json_field("factors_json"),
        "strength": parse_json_field("strength_json"),
        "updated_at": row.get("updated_at"),
        "expires_at": row.get("expires_at"),
        "strength_generated_at": row.get("strength_generated_at"),
        "strength_workflow_rev": row.get("strength_workflow_rev"),
    }


def _refresh_cached_brand_elements(brand_keys: list[str]) -> None:
    if not brand_keys:
        return
    try:
        from pipeline.scripts.etl import cache_brand_elements

        os.environ.setdefault("MARIADB_DATABASE", cache_brand_elements.TARGET_DATABASE)
        conn = cache_brand_elements.connect_db()
        try:
            cache_brand_elements.ensure_cache_brand_elements_table(conn)
            payloads = cache_brand_elements.build_brand_element_payloads(
                conn,
                brand_keys,
                agent3_schema=get_settings().agent3_db_name,
            )
            cache_brand_elements.upsert_brand_elements(conn, payloads)
        finally:
            conn.close()
    except Exception:
        logger.warning("cache_brand_elements refresh failed", exc_info=True)


def _load_cached_brand_elements(brand_keys: list[str]) -> dict[str, dict]:
    keys = [key for key in dict.fromkeys(str(value) for value in brand_keys if str(value).strip())]
    if not keys:
        return {}
    placeholders = ", ".join(["%s"] * len(keys))
    try:
        rows = db.fetch_all(
            f"""
            SELECT brand_key, brand_name, factors_json, strength_json,
                   strength_generated_at, strength_workflow_rev, updated_at,
                   source_computed_at, expires_at
            FROM cache_brand_elements
            WHERE brand_key IN ({placeholders})
            """,
            keys,
        )
        rows_by_key = {str(row.get("brand_key")): row for row in rows}
        stale_keys = [
            key
            for key in keys
            if key in rows_by_key
            and not _row_cache_fresh(
                rows_by_key[key],
                ttl_days=_ttl_days("BRAND_ELEMENTS_CACHE_TTL_DAYS", BRAND_ELEMENTS_CACHE_TTL_DAYS),
            )
        ]
        if stale_keys:
            _refresh_cached_brand_elements(stale_keys)
            rows = db.fetch_all(
                f"""
                SELECT brand_key, brand_name, factors_json, strength_json,
                       strength_generated_at, strength_workflow_rev, updated_at,
                       source_computed_at, expires_at
                FROM cache_brand_elements
                WHERE brand_key IN ({placeholders})
                """,
                keys,
            )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return {}
        else:
            raise
    except pymysql.MySQLError:
        logger.warning("cache_brand_elements lookup failed", exc_info=True)
        return {}
    return {str(row.get("brand_key")): _parse_cached_brand_element(row) for row in rows}


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


def _first_factor_atc4(factors: dict) -> str:
    atc_values = factors.get("atc")
    if not isinstance(atc_values, list):
        return ""
    return next((str(value).strip() for value in atc_values if str(value).strip()), "")


def _strategic_ml_id(market_id: str) -> str:
    if market_id.startswith("strategy_"):
        return f"ml_{market_id.removeprefix('strategy_')}"
    return market_id


def _resolve_brand_factor_choices(
    row: dict,
    requested_brand: str,
    atc4: str | None,
    selected_factors: dict,
) -> dict[str, tuple]:
    matched_brand = str(row.get("brand") or row.get("brand_key") or requested_brand)
    selected_brand_key = str(row.get("brand_key") or matched_brand)
    market_atc4 = str(row.get("atc4_code") or atc4 or _first_factor_atc4(selected_factors)).strip()
    strategic_market_id = _strategic_ml_id(str(row.get("market_id") or "").strip())
    fallback = fallback_brand_choices(selected_brand_key or matched_brand, matched_brand)
    if not selected_brand_key:
        return {source: fallback for source in BRAND_FACTOR_DB_SOURCES}
    if not market_atc4 and not strategic_market_id and "brand_key" not in row:
        return {source: fallback for source in BRAND_FACTOR_DB_SOURCES}

    choices_by_source: dict[str, tuple] = {}
    for response_source, db_source in BRAND_FACTOR_DB_SOURCES.items():
        try:
            if "brand_key" in row and market_atc4:
                resolution = resolve_brand_set(
                    view_name="general",
                    market_id=market_atc4,
                    selected_brand=selected_brand_key,
                    filter_payload={"atc4": [market_atc4]},
                    source=db_source,
                    rank_by_latest_period=True,
                )
            else:
                resolution = resolve_brand_set(
                    view_name="strategic_ml",
                    market_id=strategic_market_id or None,
                    selected_brand=selected_brand_key,
                    filter_payload={},
                    source=db_source,
                    rank_by_latest_period=True,
                )
        except (BrandSetInputError, BrandSetResolutionError, KeyError, ValueError, IndexError, pymysql.MySQLError):
            logger.info(
                "deep-analysis source market resolver fell back to selected brand only: source=%s",
                response_source,
                exc_info=True,
            )
            resolution = None
        choices_by_source[response_source] = resolution.choices if resolution and resolution.choices else fallback
    return choices_by_source


def _fetch_deep_analysis_row(brand: str) -> dict | None:
    try:
        row = db.fetch_one(
            """
            SELECT brand, market_id, response_json, brand_factors, updated_at
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
            SELECT brand, market_id, response_json, brand_factors, updated_at
            FROM cache_deep_analysis
            WHERE {_compact_sql("brand")} = %s
            LIMIT 2
            """,
            [compact],
        )
        return _single_compact_row(rows, raise_on_ambiguous=True, brand=brand)
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
            return _single_compact_row(rows, raise_on_ambiguous=True, brand=brand)
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
            SELECT brand_key, brand, response_json, brand_factors, updated_at,
                   source_computed_at, expires_at, is_stale, stale_reason,
                   stale_marked_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE (brand = %s OR brand_key = %s)
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 1
            """,
            params,
        )
        if row:
            return row if _general_cache_row_fresh(row, brand, atc4) else None
        compact = compact_brand_name(brand)
        if not compact or compact == brand:
            return None
        compact_params: list[str] = [compact, compact]
        if atc4:
            compact_params.append(atc4)
        rows = db.fetch_all(
            f"""
            SELECT brand_key, brand, response_json, brand_factors, updated_at,
                   source_computed_at, expires_at, is_stale, stale_reason,
                   stale_marked_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE ({_compact_sql("brand")} = %s OR brand_key = %s)
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 2
            """,
            compact_params,
        )
        row = _single_compact_row(rows, raise_on_ambiguous=True, brand=brand)
        return row if row and _general_cache_row_fresh(row, brand, atc4) else None
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
        raise


def _general_cache_row_from_built(row: Any) -> dict:
    return {
        "response_json": row.response_json,
        "brand_factors": row.brand_factors,
        "updated_at": datetime.now(KST),
        "source_computed_at": getattr(row, "source_computed_at", None),
        "expires_at": getattr(row, "expires_at", None),
        "is_stale": getattr(row, "is_stale", 0),
        "stale_reason": getattr(row, "stale_reason", None),
        "stale_marked_at": getattr(row, "stale_marked_at", None),
        "atc4_code": row.atc4_code,
    }


def _fetch_general_deep_analysis_row_with_conn(
    conn: Any,
    brand: str,
    atc4: str | None = None,
    *,
    allow_stale: bool = False,
) -> dict | None:
    params: list[str] = [brand, brand]
    atc4_clause = ""
    if atc4:
        atc4_clause = "AND atc4_code = %s"
        params.append(atc4)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand_key, brand, response_json, brand_factors, updated_at,
                   source_computed_at, expires_at, is_stale, stale_reason,
                   stale_marked_at, atc4_code
            FROM cache_deep_analysis_general
            WHERE (brand = %s OR brand_key = %s)
              {atc4_clause}
            ORDER BY atc4_code ASC
            LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
    if not row:
        return None
    if allow_stale:
        return row
    return row if _general_cache_row_fresh_with_conn(conn, row, brand, atc4) else None


def _latest_general_source_computed_at(brand: str, atc4: str | None) -> datetime | None:
    params: list[str] = [brand, brand]
    atc4_clause = ""
    if atc4:
        atc4_clause = "AND atc4_code = %s"
        params.append(atc4)
    try:
        row = db.fetch_one(
            f"""
            SELECT MAX(computed_at) AS source_computed_at
            FROM mart_general_brand_metric
            WHERE (brand_name = %s OR brand_key = %s)
              {atc4_clause}
            """,
            params,
        )
    except pymysql.MySQLError:
        return None
    value = row.get("source_computed_at") if row else None
    return _coerce_datetime(value)


def _latest_general_source_computed_at_with_conn(conn: Any, brand: str, atc4: str | None) -> datetime | None:
    params: list[str] = [brand, brand]
    atc4_clause = ""
    if atc4:
        atc4_clause = "AND atc4_code = %s"
        params.append(atc4)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT MAX(computed_at) AS source_computed_at
            FROM mart_general_brand_metric
            WHERE (brand_name = %s OR brand_key = %s)
              {atc4_clause}
            """,
            params,
        )
        row = cur.fetchone()
    value = row.get("source_computed_at") if row else None
    return _coerce_datetime(value)


def _general_cache_row_fresh(row: dict, brand: str, atc4: str | None) -> bool:
    if _row_marked_stale(row):
        return False
    cached_source = _coerce_datetime(row.get("source_computed_at"))
    if cached_source is None:
        return True
    latest_source = _latest_general_source_computed_at(brand, atc4 or str(row.get("atc4_code") or "").strip() or None)
    return not (cached_source is not None and latest_source is not None and latest_source > cached_source)


def _general_cache_row_fresh_with_conn(conn: Any, row: dict, brand: str, atc4: str | None) -> bool:
    if _row_marked_stale(row):
        return False
    cached_source = _coerce_datetime(row.get("source_computed_at"))
    if cached_source is None:
        return True
    latest_source = _latest_general_source_computed_at_with_conn(conn, brand, atc4 or str(row.get("atc4_code") or "").strip() or None)
    return not (cached_source is not None and latest_source is not None and latest_source > cached_source)


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
    conn = None
    lock_name = _general_forecast_lock_name(brand, atc4)
    lock_acquired = False
    try:
        conn = general_builder.mariadb_connect()
        general_builder.assert_d2_database(conn)
        general_builder.ensure_general_cache_table(conn)
        stale_row = _fetch_general_deep_analysis_row_with_conn(conn, brand, atc4, allow_stale=True)
        lock_acquired = _acquire_general_forecast_lock(conn, lock_name)
        if not lock_acquired:
            row = _fetch_general_deep_analysis_row_with_conn(conn, brand, atc4)
            if row:
                return row
            if stale_row:
                return stale_row
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
        if conn is not None:
            stale_row = _fetch_general_deep_analysis_row_with_conn(conn, brand, atc4, allow_stale=True)
            if stale_row:
                return stale_row
        raise GeneralForecastUnavailable(brand=brand, atc4=atc4, reason="forecast_generation_failed") from exc
    finally:
        if conn is not None and lock_acquired:
            _release_general_forecast_lock(conn, lock_name)
        if conn is not None:
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
    view: Annotated[
        Literal["general", "strategy"],
        Query(description="심층분석 view 선택자입니다. general 또는 strategy를 지정합니다."),
    ] = "strategy",
) -> dict:
    brand = unquote(brand_name)
    try:
        row = _fetch_general_deep_analysis_row(brand, atc4) if atc4 else _fetch_deep_analysis_row(brand)
        if not row and not atc4:
            row = _fetch_general_deep_analysis_row(brand)
    except CompactBrandLookupAmbiguous as exc:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand}) from exc
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
        matched_brand = str(row.get("brand") or row.get("brand_key") or brand)
        selected_brand_key = str(row.get("brand_key") or matched_brand)
        selected_factors = _load_brand_factors(row.get("brand_factors"))
        brand_choices_by_source = _resolve_brand_factor_choices(row, brand, atc4, selected_factors)
        data["ai_analysis"] = _load_ai_analysis(matched_brand)
        ai_analysis_short, ai_analysis_long = _load_ai_analysis_variants(matched_brand)
        data["ai_analysis_short"] = ai_analysis_short
        data["ai_analysis_long"] = ai_analysis_long
        brand_keys = sorted(
            {
                choice.brand_key
                for choices in brand_choices_by_source.values()
                for choice in choices
            }
        )
        cached_elements = _load_cached_brand_elements(brand_keys)
        strength_by_source = _load_brand_strength_by_source(brand_keys)
        data.pop("brand_elements", None)
        data.pop("strength_by_source", None)
        data["brand_factors"] = build_brand_factors(
            brand_choices_by_source,
            selected_brand_key=selected_brand_key,
            cached_elements_by_key=cached_elements,
            selected_factors=selected_factors,
            strength_by_source_by_key=strength_by_source,
        )
    return payload
