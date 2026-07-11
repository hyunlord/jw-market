from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from typing import Annotated, Any, Final
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
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
from pipeline.scripts.api.deep_analysis_runtime import build_strategic_row, load_events
from pipeline.scripts.api.dynamic_market.response_cache import DynamicMarketOverloadedError, normalize_json_value
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import get_settings
from pipeline.scripts.api.openapi_docs import DEEP_ANALYSIS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


router = APIRouter()
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
FORECAST_HORIZON_QUARTERS = 20
FORECAST_HORIZON_MONTHS = 60
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


def _fetch_general_metric_rows(brand: str) -> list[dict]:
    base_sql = """
        SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure,
               metric_history, unit_label, computed_at
        FROM mart_general_brand_metric
        WHERE brand_name = %s OR brand_key = %s
        ORDER BY atc4_code, source, measure
    """
    rows = db.fetch_all(base_sql, [brand, brand])
    if rows:
        return rows
    compact = compact_brand_name(brand)
    if not compact or compact == brand:
        return []
    return db.fetch_all(
        f"""
        SELECT brand_key, brand_name, atc4_code, atc4_desc, source, measure,
               metric_history, unit_label, computed_at
        FROM mart_general_brand_metric
        WHERE {_compact_sql("brand_name")} = %s OR {_compact_sql("brand_key")} = %s
        ORDER BY atc4_code, source, measure
        """,
        [compact, compact],
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


def _general_row_from_mart(brand: str, *, is_jw: bool = False) -> dict | None:
    rows = _fetch_general_metric_rows(brand)
    if not rows:
        return None
    selected_atc4 = _choose_general_atc4(rows)
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
    row = db.fetch_one(
        """
        SELECT MAX(is_jw) AS is_jw, MAX(is_target) AS is_target
        FROM mart_strategic_ml_brand_metric
        WHERE brand_name = %s
        """,
        [brand],
    )
    return bool(row and row.get("is_jw")), bool(row and row.get("is_target"))


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
    request: Request = None,
    view: Annotated[
        str,
        Query(description="[입력] general 또는 strategic. 생략 시 기존 소비자 호환을 위해 strategic으로 처리합니다."),
    ] = "strategic",
) -> dict:
    if request is not None and "atc4" in request.query_params:
        raise HTTPException(
            status_code=422,
            detail={"error": "unsupported_query_parameter", "parameter": "atc4", "message": "atc4 is derived by the backend"},
        )
    brand = unquote(brand_name)
    view_name = _normalize_deep_view(view)
    try:
        if view_name == "general":
            payload, row = _compose_general_view_payload(brand)
        else:
            payload, row = _compose_strategic_view_payload(brand)
    except CompactBrandLookupAmbiguous as exc:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand}) from exc
    payload["generated_at"] = _format_generated_at(row.get("updated_at"))
    _slice_forecast_horizon(payload)
    data = payload.setdefault("data", {})
    if isinstance(data, dict):
        matched_brand = str(row.get("brand") or row.get("brand_key") or brand)
        cached_events = row.get("_events")
        events = cached_events if isinstance(cached_events, list) else _load_deep_events(matched_brand)
        if events or "events" in data:
            data["events"] = events
        selected_brand_key = str(row.get("brand_key") or matched_brand)
        selected_factors = _load_brand_factors(row.get("brand_factors"))
        brand_choices_by_source = _resolve_brand_factor_choices(row, brand, None, selected_factors)
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
    non_finite_paths: list[str] = []
    normalized = normalize_json_value(payload, on_non_finite=non_finite_paths.append)
    if non_finite_paths:
        logger.warning("deep_analysis_non_finite_normalized brand=%s view=%s paths=%s", brand, view_name, non_finite_paths)
    json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    return normalized
