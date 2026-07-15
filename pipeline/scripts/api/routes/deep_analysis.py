from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import os
from typing import Annotated, Any, Final, Literal
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandSetInputError,
    BrandSetResolutionError,
    resolve_brand_set,
)
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.deep_analysis_brand_elements import (
    build_brand_factors,
    fallback_brand_choices,
)
from pipeline.scripts.api.deep_analysis_context import (
    DeepAnalysisContext,
    DeepAnalysisContextError,
    public_source_labels,
    resolve_deep_analysis_context,
)
from pipeline.scripts.api.deep_analysis_serving import (
    load_forecast_block,
    load_market_strength_records,
)
from pipeline.scripts.api.deep_analysis_runtime import build_strategic_row, load_events
from pipeline.scripts.api.dynamic_market.response_cache import DynamicMarketOverloadedError, normalize_json_value
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import get_settings
from pipeline.scripts.api.openapi_docs import DEEP_ANALYSIS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


router = APIRouter()
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
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


_AI_GENERATION_STATUS_PRIORITY = {
    "complete": 0,
    "complete_template_fallback": 1,
    "legacy_unbound": 2,
}


def _unavailable_canonical_ai_variant(status: str = "not_generated") -> dict:
    payload = _not_generated_ai_variant()
    payload.update(
        generation_status=status,
        generated_at=None,
        timestamp_status="unknown",
    )
    return payload


def _format_origin_generated_at(value: object) -> str | None:
    if isinstance(value, datetime):
        generated_at = value
    else:
        try:
            generated_at = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=KST)
    else:
        generated_at = generated_at.astimezone(KST)
    return generated_at.isoformat(timespec="seconds")


def _canonical_ai_variant(rows: list[dict], variant: str) -> dict:
    status_key = f"{variant}_generation_status"
    generated_at_key = f"{variant}_generated_at"
    payload_key = f"ai_analysis_{variant}_json"
    candidates = sorted(
        rows,
        key=lambda row: (
            _AI_GENERATION_STATUS_PRIORITY.get(str(row.get(status_key) or ""), 3),
            str(row.get("brand") or ""),
        ),
    )
    if not candidates:
        return _unavailable_canonical_ai_variant()
    selected = candidates[0]
    payload = _parse_ai_variant(selected.get(payload_key))
    status = str(selected.get(status_key) or "unknown")
    generated_at = _format_origin_generated_at(selected.get(generated_at_key))
    payload["generation_status"] = status
    payload["generated_at"] = generated_at
    if generated_at is None:
        payload["timestamp_status"] = "unknown"
    return payload


def _load_canonical_ai_analysis_variants(brand: str) -> tuple[dict, dict]:
    columns = """
        brand, ai_analysis_short_json, ai_analysis_long_json,
        short_generation_status, short_generated_at,
        long_generation_status, long_generated_at
    """
    try:
        rows = db.fetch_all(
            f"SELECT {columns} FROM cache_deep_analysis_ai_analysis WHERE brand = %s",
            [brand],
        )
        if not rows:
            compact = compact_brand_name(brand)
            if compact and compact != brand:
                rows = db.fetch_all(
                    f"""
                    SELECT {columns}
                    FROM cache_deep_analysis_ai_analysis
                    WHERE {_compact_sql("brand")} = %s
                    """,
                    [compact],
                )
                if len({str(row.get("brand") or "") for row in rows}) > 1:
                    rows = []
    except pymysql.err.ProgrammingError as exc:
        if not exc.args or exc.args[0] != 1054:
            logger.warning("canonical AI analysis lookup failed", exc_info=True)
            return _unavailable_canonical_ai_variant(), _unavailable_canonical_ai_variant()
        short, long = _load_ai_analysis_variants(brand)
        for payload in (short, long):
            payload["generation_status"] = "unknown"
            payload["generated_at"] = None
            payload["timestamp_status"] = "unknown"
        return short, long
    except pymysql.MySQLError:
        logger.warning("canonical AI analysis lookup failed", exc_info=True)
        return _unavailable_canonical_ai_variant(), _unavailable_canonical_ai_variant()
    return _canonical_ai_variant(rows, "short"), _canonical_ai_variant(rows, "long")


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
    *,
    candidate_cache: dict[tuple[str, str, str], object] | None = None,
) -> tuple[dict[str, tuple], dict[str, dict[str, object]]]:
    matched_brand = str(row.get("brand") or row.get("brand_key") or requested_brand)
    selected_brand_key = str(row.get("brand_key") or matched_brand)
    market_atc4 = str(row.get("atc4_code") or atc4 or _first_factor_atc4(selected_factors)).strip()
    strategic_market_id = _strategic_ml_id(str(row.get("market_id") or "").strip())
    fallback = fallback_brand_choices(selected_brand_key or matched_brand, matched_brand)
    if not selected_brand_key:
        return (
            {source: fallback for source in BRAND_FACTOR_DB_SOURCES},
            {source: {"available": True, "reason": None} for source in BRAND_FACTOR_DB_SOURCES},
        )

    choices_by_source: dict[str, tuple] = {}
    meta_by_source: dict[str, dict[str, object]] = {}
    for response_source in BRAND_FACTOR_DB_SOURCES:
        try:
            if strategic_market_id:
                context = resolve_deep_analysis_context(
                    brand=selected_brand_key,
                    view_kind="strategic_ml",
                    market_id=strategic_market_id,
                    source=response_source,
                    _candidate_cache=candidate_cache,
                )
                choices = _resolve_context_brand_choices(context)
            elif market_atc4:
                resolution = resolve_brand_set(
                    view_name="general",
                    market_id=market_atc4,
                    selected_brand=selected_brand_key,
                    filter_payload={"atc4": [market_atc4]},
                    source=BRAND_FACTOR_DB_SOURCES[response_source],
                    rank_by_latest_period=True,
                )
                choices = resolution.choices if resolution and resolution.choices else fallback
            else:
                choices = fallback
        except DeepAnalysisContextError as exc:
            logger.warning(
                "deep_analysis_brand_factor_market_resolve_failed "
                "brand=%s market_id=%s source=%s view=%s error=%s",
                selected_brand_key,
                strategic_market_id or market_atc4,
                response_source,
                "strategic_ml" if strategic_market_id else "general",
                exc.error,
            )
            choices = () if strategic_market_id else fallback
        except (
            BrandSetInputError,
            BrandSetResolutionError,
            KeyError,
            ValueError,
            IndexError,
            pymysql.MySQLError,
        ):
            logger.warning(
                "deep_analysis_brand_factor_market_resolve_failed brand=%s market_id=%s source=%s",
                selected_brand_key,
                strategic_market_id or market_atc4,
                response_source,
                exc_info=True,
            )
            choices = () if strategic_market_id else fallback
        if choices:
            choices_by_source[response_source] = choices
            meta_by_source[response_source] = {"available": True, "reason": None}
        elif strategic_market_id:
            logger.warning(
                "deep_analysis_brand_factor_market_resolve_failed brand=%s market_id=%s source=%s reason=no_choices",
                selected_brand_key,
                strategic_market_id,
                response_source,
            )
            choices_by_source[response_source] = ()
            meta_by_source[response_source] = {"available": False, "reason": "market_resolve_failed"}
        else:
            choices_by_source[response_source] = fallback
            meta_by_source[response_source] = {"available": True, "reason": None}
    return choices_by_source, meta_by_source


def _resolve_context_brand_choices(context: DeepAnalysisContext) -> tuple:
    resolution = resolve_brand_set(
        view_name=context.view_kind,
        market_id=context.market_id,
        selected_brand=context.brand_key,
        filter_payload={"atc4": [context.market_id]} if context.view_kind == "general" else {},
        source=context.db_source,
        rank_by_latest_period=True,
        resolved_context=context,
    )
    return resolution.choices if resolution and resolution.choices else ()


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


def _general_cache_row_fresh(row: dict, brand: str, atc4: str | None) -> bool:
    if _row_marked_stale(row):
        return False
    cached_source = _coerce_datetime(row.get("source_computed_at"))
    if cached_source is None:
        return True
    latest_source = _latest_general_source_computed_at(brand, atc4 or str(row.get("atc4_code") or "").strip() or None)
    return not (cached_source is not None and latest_source is not None and latest_source > cached_source)


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _history_points(value: object) -> list[tuple[str, float | None, float | None]]:
    history = _json_object(value)
    points: list[tuple[str, float | None, float | None]] = []
    for period, item in sorted(history.items()):
        if not isinstance(item, dict):
            continue
        raw = item.get("raw_value")
        ms = item.get("ms")
        try:
            raw_value = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            raw_value = None
        try:
            ms_value = float(ms) if ms is not None else None
        except (TypeError, ValueError):
            ms_value = None
        points.append((str(period), raw_value, ms_value))
    return points


def _recent_history_value(row: dict) -> float:
    points = _history_points(row.get("metric_history"))
    for _period, raw_value, _ms_value in reversed(points):
        if raw_value is not None:
            return raw_value
    return 0.0


def _fetch_general_metric_rows(
    brand: str,
    *,
    atc4: str | None = None,
    source: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[str] = [brand, brand]
    if atc4:
        clauses.append("atc4_code = %s")
        params.append(atc4)
    if source:
        clauses.append("source = %s")
        params.append(source)
    scope_sql = "".join(f" AND {clause}" for clause in clauses)
    base_sql = f"""
        SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure,
               metric_history, unit_label, computed_at
        FROM mart_general_brand_metric
        WHERE (brand_name = %s OR brand_key = %s)
        {scope_sql}
        ORDER BY atc4_code, source, measure
    """
    rows = db.fetch_all(base_sql, params)
    if rows:
        return rows
    compact = compact_brand_name(brand)
    if not compact or compact == brand:
        return []
    compact_params: list[str] = [compact, compact]
    if atc4:
        compact_params.append(atc4)
    if source:
        compact_params.append(source)
    return db.fetch_all(
        f"""
        SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure,
               metric_history, unit_label, computed_at
        FROM mart_general_brand_metric
        WHERE ({_compact_sql("brand_name")} = %s OR {_compact_sql("brand_key")} = %s)
        {scope_sql}
        ORDER BY atc4_code, source, measure
        """,
        compact_params,
    )


def _choose_general_atc4(rows: list[dict]) -> str:
    totals: dict[str, float] = {}
    for row in rows:
        atc4 = str(row.get("atc4_code") or "").strip()
        if not atc4:
            continue
        totals[atc4] = totals.get(atc4, 0.0) + _recent_history_value(row)
    if not totals:
        return ""
    return sorted(totals, key=lambda value: (-totals[value], value))[0]


def _general_metric_row_to_combo(row: dict, *, target_brand: str) -> dict:
    points = _history_points(row.get("metric_history"))
    periods = [period for period, _raw, _ms in points]
    values = [raw for _period, raw, _ms in points]
    ms_values = [ms for _period, _raw, ms in points]
    source = str(row.get("source") or "")
    period_unit = "분기" if source == "iqvia_nsa" else "월"
    return {
        "period_unit": period_unit,
        "unit_label": row.get("unit_label"),
        "history_periods": periods,
        "forecast_periods": [],
        "forecast_values": [],
        "forecast_ms_pct": [],
        "target_brand": row.get("brand_name") or target_brand,
        "brands": [
            {
                "brand": row.get("brand_name") or target_brand,
                "is_target": True,
                "history_periods": periods,
                "history_values": values,
                "history_ms_pct": ms_values,
                "forecast_values": [],
                "forecast_ms_pct": [],
                "forecast_intervals": {},
            }
        ],
        "baseline": {
            "value_recent": values[-1] if values else None,
            "ms_recent_pct": ms_values[-1] if ms_values else None,
        },
        "forecast_intervals": {},
    }


def _general_row_from_mart(
    brand: str,
    *,
    is_jw: bool = False,
    atc4: str | None = None,
    source: str | None = None,
) -> dict | None:
    rows = _fetch_general_metric_rows(brand, atc4=atc4, source=source)
    if not rows:
        return None
    selected_atc4 = atc4 or _choose_general_atc4(rows)
    selected_rows = [row for row in rows if str(row.get("atc4_code") or "").strip() == selected_atc4]
    if not selected_rows:
        return None
    base = selected_rows[0]
    brand_key = str(base.get("brand_key") or brand)
    brand_name = str(base.get("brand_name") or brand)
    combos: dict[str, dict] = {}
    for row in selected_rows:
        source = str(row.get("source") or "")
        measure = str(row.get("measure") or "")
        if not source or not measure:
            continue
        api_source = "IQVIA" if source == "iqvia_nsa" else source.upper()
        combos[f"{api_source}.{measure}"] = _general_metric_row_to_combo(row, target_brand=brand_name)
    payload = {
        "brand": brand_name,
        "brand_name": brand_name,
        "brand_key": brand_key,
        "market_id": f"general:{selected_atc4}",
        "market_name": base.get("atc4_desc"),
        "available_combos": sorted(combos),
        "data": {
            "forecast": {
                "method": "observed_general_mart",
                "disclaimer": "General view is assembled from mart_general_brand_metric without request-time forecast generation.",
                "is_statistical_model": False,
                "backtest_available": False,
                "event_regressor_enabled": False,
                "phase29_poc": None,
                "by_combo": combos,
            },
            "simulation": {"by_combo": {}},
            "events": [],
        },
        "market_meta": {
            "market_name": base.get("atc4_desc"),
            "atc4_code": selected_atc4,
            "atc4_name": base.get("atc4_desc"),
            "sources": sorted({("IQVIA" if str(row.get("source")) == "iqvia_nsa" else str(row.get("source") or "").upper()) for row in selected_rows}),
            "default_source": "IQVIA" if str(base.get("source")) == "iqvia_nsa" else str(base.get("source") or "").upper(),
            "available_combos": sorted(combos),
            "source_count": len({row.get("source") for row in selected_rows}),
            "measure_count": len({row.get("measure") for row in selected_rows}),
            "market_count": 1,
            "is_jw": is_jw,
            "is_target": True,
            "cache_scope": "general_mart",
            "tie_break": "latest_raw_value_desc_then_atc4_ascending",
        },
    }
    computed_values = [_coerce_datetime(row.get("computed_at")) for row in selected_rows]
    computed_values = [value for value in computed_values if value is not None]
    return {
        "brand_key": brand_key,
        "brand": brand_name,
        "response_json": json.dumps(payload, ensure_ascii=False),
        "brand_factors": json.dumps({"atc": [selected_atc4], "ubist": {}, "iqvia": {}}, ensure_ascii=False),
        "updated_at": max(computed_values, default=None),
        "atc4_code": selected_atc4,
    }


def _strategic_brand_flags(brand: str) -> tuple[bool, bool]:
    is_jw = get_display_brand(brand) is not None
    return is_jw, False


def _compose_general_view_payload(brand: str) -> tuple[dict, dict]:
    general_row = _fetch_general_deep_analysis_row(brand)
    if not general_row:
        is_jw, _is_target = _strategic_brand_flags(brand)
        general_row = _general_row_from_mart(brand, is_jw=is_jw)
    if not general_row:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})

    general_payload = compose_cached_json(general_row["response_json"])
    if not isinstance(general_payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "general_deep_analysis"})

    is_jw, _is_target = _strategic_brand_flags(brand)
    market_meta = general_payload.get("market_meta")
    if isinstance(market_meta, dict):
        market_meta["is_jw"] = is_jw

    return general_payload, general_row


def _strategic_row_from_mart(brand: str) -> dict | None:
    return build_strategic_row(brand)


def _load_deep_events(brand: str) -> list[dict]:
    return load_events(brand)


def _compose_strategic_view_payload(brand: str) -> tuple[dict, dict]:
    try:
        row = _strategic_row_from_mart(brand)
    except DynamicMarketOverloadedError as exc:
        raise HTTPException(status_code=429, detail={"error": "deep_analysis_busy"}) from exc
    if not row:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_mart_payload", "source": "mart_strategic_ml_brand_metric"})
    return payload, row


def _compose_formal_context_payload(
    brand: str,
    context: DeepAnalysisContext,
) -> tuple[dict, dict]:
    if context.view_kind == "general" and context.has_market_data:
        row = _fetch_general_deep_analysis_row(brand, context.market_id)
        if not row:
            is_jw, _is_target = _strategic_brand_flags(brand)
            row = _general_row_from_mart(
                brand,
                is_jw=is_jw,
                atc4=context.market_id,
                source=context.db_source,
            )
        if row:
            payload = compose_cached_json(row["response_json"])
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=500,
                    detail={"error": "invalid_cache_payload", "cache": "general_deep_analysis"},
                )
            _scope_formal_payload(payload, context)
            return payload, row

    payload = _empty_formal_payload(context)
    row = {
        "brand": context.brand_name,
        "brand_key": context.brand_key,
        "brand_factors": json.dumps(_empty_brand_factors(), ensure_ascii=False),
        "updated_at": datetime.now(KST),
        "market_id": context.market_id,
        "atc4_code": context.market_id if context.view_kind == "general" else None,
    }
    if context.has_market_data:
        forecast, simulation = _load_formal_forecast_sections(context)
        data = payload["data"]
        if forecast is not None:
            data["forecast"] = forecast
            if _section_has_payload(forecast):
                data["forecast_meta"] = {"status": "available", "reason": None}
            else:
                reason = forecast.get("reason") if isinstance(forecast, dict) else "not_generated"
                data["forecast_meta"] = {"status": reason, "reason": reason}
        if simulation is not None:
            data["simulation"] = simulation
    return payload, row


def _empty_formal_payload(context: DeepAnalysisContext) -> dict:
    status = "no_market_data" if not context.has_market_data else "not_generated"
    reason = (
        "해당 시장/소스에서 매출 데이터 없음"
        if not context.has_market_data
        else "해당 시장 컨텍스트의 예측 데이터가 아직 생성되지 않음"
    )
    data: dict[str, Any] = {
        "forecast": {"available": False, "reason": status},
        "simulation": {"available": False, "reason": status},
        "events": [],
        "forecast_meta": {"status": status, "reason": reason},
        "strength_meta": {
            "status": "not_generated",
            "reason": "해당 시장 컨텍스트의 강점 데이터가 아직 생성되지 않음",
        },
    }
    if not context.has_market_data:
        data["data_meta"] = _no_market_data_meta(context)
    return {
        "brand": context.brand_name,
        "brand_name": context.brand_name,
        "brand_key": context.brand_key,
        "view_kind": context.view_kind,
        "source": context.source,
        "market_id": context.market_id,
        "market_name": context.market_name,
        "data": data,
        "market_meta": _formal_market_meta(context),
    }


def _formal_market_meta(context: DeepAnalysisContext) -> dict:
    meta = {
        "market_id": context.market_id,
        "market_name": context.market_name,
        "view_kind": context.view_kind,
        "source": context.source,
        "sources": list(context.market_allowed_sources),
        "default_source": context.source,
        "market_allowed_sources": list(context.market_allowed_sources),
        "brand_available_sources": list(context.brand_available_sources),
        "in_catalog": context.in_catalog,
        "has_market_data": context.has_market_data,
    }
    if (
        not context.has_market_data
        and context.db_source not in context.brand_available_sources
        and context.brand_available_sources
    ):
        meta.update(
            available=False,
            reason="brand_not_in_source",
            available_sources=public_source_labels(context.brand_available_sources),
        )
    return meta


def _scope_formal_payload(payload: dict, context: DeepAnalysisContext) -> None:
    payload["view_kind"] = context.view_kind
    payload["source"] = context.source
    payload["market_id"] = context.market_id
    payload["market_name"] = context.market_name
    payload["brand"] = context.brand_name
    payload["brand_name"] = context.brand_name
    payload["brand_key"] = context.brand_key
    data = payload.setdefault("data", {})
    if not isinstance(data, dict):
        payload["data"] = data = {}
    prefix = "IQVIA." if context.source == "iqvia" else "UBIST."
    for section_name in ("forecast", "simulation"):
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        by_combo = section.get("by_combo")
        if isinstance(by_combo, dict):
            section["by_combo"] = {
                key: value
                for key, value in by_combo.items()
                if str(key).upper().startswith(prefix)
            }
    if _section_has_payload(data.get("forecast")):
        data["forecast_meta"] = {"status": "available", "reason": None}
    else:
        unavailable = data.get("forecast")
        reason = (unavailable.get("reason") if isinstance(unavailable, dict) else None) or "not_generated"
        data["forecast"] = {"available": False, "reason": reason}
        data["forecast_meta"] = {
            "status": reason,
            "reason": "해당 시장 컨텍스트의 예측 데이터가 아직 생성되지 않음",
        }
    if not _section_has_payload(data.get("simulation")):
        unavailable = data.get("simulation")
        reason = (unavailable.get("reason") if isinstance(unavailable, dict) else None) or "not_generated"
        data["simulation"] = {"available": False, "reason": reason}
    data.setdefault(
        "strength_meta",
        {"status": "not_generated", "reason": "해당 시장 컨텍스트의 강점 데이터가 아직 생성되지 않음"},
    )
    existing_market_meta = payload.get("market_meta")
    if not isinstance(existing_market_meta, dict):
        existing_market_meta = {}
    payload["market_meta"] = {**existing_market_meta, **_formal_market_meta(context)}


def _section_has_payload(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    if not isinstance(value, dict):
        return False
    if value.get("available") is False:
        return False
    by_combo = value.get("by_combo")
    return bool(by_combo) if isinstance(by_combo, dict) else bool(value)


def _no_market_data_meta(context: DeepAnalysisContext) -> dict:
    market_sources = list(context.market_allowed_sources)
    brand_sources = list(context.brand_available_sources)
    reason = "해당 시장/소스에서 매출 데이터 없음"
    if context.db_source not in context.brand_available_sources and market_sources and brand_sources:
        market_label = " + ".join(source.upper() for source in market_sources)
        brand_label = " + ".join(
            "IQVIA" if source == "iqvia_nsa" else source.upper()
            for source in brand_sources
        )
        reason = f"해당 시장은 {market_label} 기준이나 이 브랜드는 {brand_label} 데이터만 존재"
    return {
        "status": "no_market_data",
        "reason": reason,
        "market_allowed_sources": market_sources,
        "brand_available_sources": brand_sources,
        "in_catalog": context.in_catalog,
    }


def _load_formal_forecast_sections(context: DeepAnalysisContext) -> tuple[object | None, object | None]:
    block = load_forecast_block(context)
    if block is None:
        return None, None
    return block.forecast, block.simulation


def _load_cached_brand_elements_read_only(brand_keys: list[str]) -> dict[str, dict]:
    keys = [key for key in dict.fromkeys(brand_keys) if key]
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
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return {}
        raise
    except pymysql.MySQLError:
        logger.warning("formal brand element lookup failed", exc_info=True)
        return {}
    return {str(row.get("brand_key")): _parse_cached_brand_element(row) for row in rows}


def _load_market_strength(
    brand_keys: list[str],
    context: DeepAnalysisContext,
) -> dict[str, dict[str, dict]]:
    rows = load_market_strength_records(brand_keys, context)
    result: dict[str, dict[str, dict]] = {}
    for row in rows:
        parsed = _parse_source_brand_strength(row)
        if parsed:
            result.setdefault(str(row.get("brand_key") or ""), {})[context.source] = parsed
    return result


def _formal_brand_factors(brand: str, context: DeepAnalysisContext) -> dict[str, list[dict]]:
    fallback = fallback_brand_choices(context.brand_key, context.brand_name)
    try:
        choices = _resolve_context_brand_choices(context)
    except (BrandSetInputError, BrandSetResolutionError, KeyError, ValueError, IndexError, pymysql.MySQLError):
        logger.info("formal deep-analysis brand-factor resolver unavailable", exc_info=True)
        choices = fallback
    if not choices:
        choices = fallback
    brand_keys = [choice.brand_key for choice in choices]
    cached = _load_cached_brand_elements_read_only(brand_keys)
    strengths = _load_market_strength(brand_keys, context)
    choices_by_source = {"iqvia": (), "ubist": (), context.source: choices}
    return build_brand_factors(
        choices_by_source,
        selected_brand_key=context.brand_key,
        cached_elements_by_key=cached,
        selected_factors=_empty_brand_factors(),
        strength_by_source_by_key=strengths,
    )


def _brand_factors_have_strength(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return any(
        isinstance(item, dict) and bool(item.get("strength"))
        for items in value.values()
        if isinstance(items, list)
        for item in items
    )


def _normalize_deep_view(view: str | None) -> str:
    normalized = (view or "strategic").strip().lower()
    if normalized in {"", "strategic"}:
        return "strategic"
    if normalized == "general":
        return "general"
    raise HTTPException(
        status_code=422,
        detail={"error": "invalid_view", "allowed": ["general", "strategic"], "value": view},
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


@router.get(
    "/api/deep-analysis/{brand_name}",
    tags=[PORTAL_CORE_TAG],
    summary="포탈 심층분석 조회",
    description=(
        "view_kind, market_id, source로 검증한 mart/catalog 컨텍스트에서 심층분석 payload를 반환합니다. "
        "기존 view 호출은 전환 기간 동안 유지하며 예측과 시뮬레이션은 동일한 사전 계산 block을 사용합니다."
    ),
    response_model=None,
    responses=DEEP_ANALYSIS_RESPONSES,
)
def deep_analysis(
    brand_name: str,
    request: Request = None,
    view: Annotated[
        str | None,
        Query(
            description=(
                "[입력] general 또는 strategic. 생략 시 기존 소비자 호환을 위해 "
                "strategic으로 처리합니다."
            )
        ),
    ] = None,
    view_kind: Annotated[
        Literal["general", "strategic_ml", "strategic_cd"] | None,
        Query(description="[신규 계약] general, strategic_ml, strategic_cd 중 하나"),
    ] = None,
    market_id: Annotated[
        str | None,
        Query(description="[신규 계약] general=ATC4, strategic_ml=ml_id, strategic_cd=cd_id"),
    ] = None,
    source: Annotated[
        Literal["ubist", "iqvia"] | None,
        Query(description="[신규 계약] 요청 시장의 단일 데이터 소스"),
    ] = None,
) -> dict:
    if request is not None and "atc4" in request.query_params:
        raise HTTPException(
            status_code=422,
            detail={"error": "unsupported_query_parameter", "parameter": "atc4", "message": "atc4 is derived by the backend"},
        )
    brand = unquote(brand_name)
    formal_contract = view_kind is not None or market_id is not None or source is not None
    context: DeepAnalysisContext | None = None
    if formal_contract and view is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "conflicting_view_contract",
                "message": "legacy view cannot be combined with view_kind, market_id, or source",
            },
        )
    if formal_contract and view_kind is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "missing_view_kind", "message": "view_kind is required for the formal contract"},
        )
    view_name = str(view_kind) if formal_contract else _normalize_deep_view(view)
    try:
        if formal_contract:
            try:
                context = resolve_deep_analysis_context(
                    brand=brand,
                    view_kind=str(view_kind),
                    market_id=market_id,
                    source=source,
                )
            except DeepAnalysisContextError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
            payload, row = _compose_formal_context_payload(brand, context)
        elif view_name == "general":
            payload, row = _compose_general_view_payload(brand)
        else:
            payload, row = _compose_strategic_view_payload(brand)
    except CompactBrandLookupAmbiguous as exc:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand}) from exc
    payload["generated_at"] = _format_generated_at(row.get("updated_at"))
    data = payload.setdefault("data", {})
    if isinstance(data, dict):
        matched_brand = str(row.get("brand") or row.get("brand_key") or brand)
        cached_events = row.get("_events")
        events = cached_events if isinstance(cached_events, list) else _load_deep_events(matched_brand)
        if formal_contract:
            data["events"] = events
            if not data.get("forecast"):
                data.setdefault(
                    "forecast_meta",
                    {
                        "status": "not_generated",
                        "reason": "해당 시장 컨텍스트의 예측 데이터가 아직 생성되지 않음",
                    },
                )
            if not events:
                data["events_meta"] = {
                    "status": "no_news",
                    "reason": "해당 브랜드 관련 뉴스 없음",
                    "bundle_available": False,
                }
            else:
                data["events_meta"] = {
                    "status": "available",
                    "reason": None,
                    "bundle_available": True,
                }
        elif events or "events" in data:
            data["events"] = events
        selected_brand_key = str(row.get("brand_key") or matched_brand)
        selected_factors = _load_brand_factors(row.get("brand_factors"))
        if formal_contract:
            ai_analysis_short, ai_analysis_long = _load_canonical_ai_analysis_variants(matched_brand)
        else:
            ai_analysis_short, ai_analysis_long = _load_ai_analysis_variants(matched_brand)
        if formal_contract:
            data["ai_analysis"] = ai_analysis_short
        else:
            data["ai_analysis"] = _load_ai_analysis(matched_brand)
        data["ai_analysis_short"] = ai_analysis_short
        data["ai_analysis_long"] = ai_analysis_long
        data.pop("brand_elements", None)
        data.pop("strength_by_source", None)
        if formal_contract and context is not None:
            data["brand_factors"] = _formal_brand_factors(matched_brand, context)
            if _brand_factors_have_strength(data["brand_factors"]):
                data["strength_meta"] = {"status": "available", "reason": None}
            else:
                data["strength_meta"] = {
                    "status": "not_generated",
                    "reason": "해당 시장 컨텍스트의 강점 데이터가 아직 생성되지 않음",
                }
        else:
            context_candidate_cache: dict[tuple[str, str, str], object] = {}
            resolver_kwargs = {}
            if "candidate_cache" in inspect.signature(_resolve_brand_factor_choices).parameters:
                resolver_kwargs["candidate_cache"] = context_candidate_cache
            brand_choices_by_source, brand_factor_meta = _resolve_brand_factor_choices(
                row, brand, None, selected_factors, **resolver_kwargs
            )
            unavailable_factor_sources = {
                source_name: meta
                for source_name, meta in brand_factor_meta.items()
                if meta.get("available") is False
            }
            if unavailable_factor_sources:
                data["brand_factors_meta"] = unavailable_factor_sources
            else:
                data.pop("brand_factors_meta", None)
            brand_keys = sorted(
                {
                    choice.brand_key
                    for choices in brand_choices_by_source.values()
                    for choice in choices
                }
            )
            cached_elements = _load_cached_brand_elements(brand_keys)
            strength_by_source = _load_brand_strength_by_source(brand_keys)
            data["brand_factors"] = build_brand_factors(
                brand_choices_by_source,
                selected_brand_key=selected_brand_key,
                cached_elements_by_key=cached_elements,
                selected_factors=selected_factors,
                strength_by_source_by_key=strength_by_source,
            )
    non_finite_paths: list[str] = []
    normalized = normalize_json_value(payload, on_non_finite=non_finite_paths.append)
    if non_finite_paths:
        logger.warning("deep_analysis_non_finite_normalized brand=%s view=%s paths=%s", brand, view_name, non_finite_paths)
    json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    return normalized
