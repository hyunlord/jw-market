"""Read-only adapters for the formal deep-analysis serving tables."""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContext
from pipeline.scripts.api.deep_analysis_vocabulary import STRENGTH_VIEW_KIND_BY_FORMAL_VIEW


logger = logging.getLogger(__name__)

FORECAST_BLOCK_TABLE: Final = "deep_forecast_block"
FORECAST_HORIZON_TABLE: Final = "deep_forecast_horizon"
FORECAST_MEASURES_BY_SOURCE: Final[dict[str, tuple[str, ...]]] = {
    "ubist": ("sales", "volume"),
    "iqvia_nsa": ("counting_unit", "dosage_unit", "sales", "unit"),
}
FORECAST_SOURCE_LABEL: Final[dict[str, str]] = {"ubist": "UBIST", "iqvia_nsa": "IQVIA"}
MARKET_STRENGTH_TABLE: Final = "agent3_brand_strength_market"
SOURCE_STRENGTH_TABLE: Final = "agent3_brand_strength_source"


def load_forecast_records(
    context: DeepAnalysisContext,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load brand blocks and market horizons by their distinct natural keys."""

    return (
        _load_forecast_block(context),
        _load_forecast_horizon(context),
    )


def load_market_strength_records(
    brand_keys: list[str],
    context: DeepAnalysisContext,
) -> list[dict[str, Any]]:
    """Load market-scoped Agent3 rows without legacy brand-only fallback."""

    keys = [key for key in dict.fromkeys(brand_keys) if key]
    if not keys:
        return []
    placeholders = ", ".join(["%s"] * len(keys))
    try:
        if context.view_kind == "general":
            return db.fetch_all(
                f"""
                SELECT *
                FROM {SOURCE_STRENGTH_TABLE}
                WHERE brand_key IN ({placeholders}) AND source = %s
                """,
                [*keys, context.source],
            )
        strength_view_kind = STRENGTH_VIEW_KIND_BY_FORMAL_VIEW[context.view_kind]
        return db.fetch_all(
            f"""
            SELECT *
            FROM {MARKET_STRENGTH_TABLE}
            WHERE brand_key IN ({placeholders})
              AND source = %s AND market_id = %s AND view_kind = %s
            """,
            [*keys, context.source, context.market_id, strength_view_kind],
        )
    except KeyError:
        logger.warning("unsupported formal strength view: %s", context.view_kind)
        return []
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return []
        raise
    except pymysql.MySQLError:
        logger.warning("market-scoped brand strength lookup failed", exc_info=True)
        return []


def _load_forecast_block(context: DeepAnalysisContext) -> dict[str, Any] | None:
    """Load a brand block by (brand_key, source, market_id)."""

    try:
        return db.fetch_one(
            f"""
            SELECT * FROM {FORECAST_BLOCK_TABLE}
            WHERE brand_key = %s AND source = %s AND market_id = %s
            LIMIT 1
            """,
            [context.brand_key, context.db_source, context.market_id],
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
        raise
    except pymysql.MySQLError:
        logger.warning("deep forecast serving lookup failed: table=%s", FORECAST_BLOCK_TABLE, exc_info=True)
        return None


def _load_forecast_horizon(context: DeepAnalysisContext) -> dict[str, Any] | None:
    """Reassemble market-level measure rows keyed by (market_id, source, measure)."""

    measures = FORECAST_MEASURES_BY_SOURCE.get(context.db_source, ())
    source_label = FORECAST_SOURCE_LABEL.get(context.db_source)
    if not measures or source_label is None:
        return None
    by_combo: dict[str, Any] = {}
    try:
        for measure in measures:
            row = db.fetch_one(
                f"""
                SELECT measure, forecast_horizon_json
                FROM {FORECAST_HORIZON_TABLE}
                WHERE market_id = %s AND source = %s AND measure = %s
                LIMIT 1
                """,
                [context.market_id, context.db_source, measure],
            )
            if not row:
                continue
            payload = row.get("forecast_horizon_json")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            if isinstance(payload, dict):
                by_combo[f"{source_label}.{measure}"] = payload
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
        raise
    except pymysql.MySQLError:
        logger.warning("deep forecast serving lookup failed: table=%s", FORECAST_HORIZON_TABLE, exc_info=True)
        return None
    if not by_combo:
        return None
    return {
        "forecast_horizon_json": json.dumps(
            {"by_combo": by_combo},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }
