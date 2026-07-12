"""Read-only adapters for the formal deep-analysis serving tables."""

from __future__ import annotations

import logging
from typing import Any, Final

import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContext


logger = logging.getLogger(__name__)

FORECAST_BLOCK_TABLE: Final = "deep_forecast_block"
FORECAST_HORIZON_TABLE: Final = "deep_forecast_horizon"
MARKET_STRENGTH_TABLE: Final = "agent3_brand_strength_market"


def load_forecast_records(
    context: DeepAnalysisContext,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load block and horizon rows independently by the formal composite key."""

    return (
        _load_forecast_record(FORECAST_BLOCK_TABLE, context),
        _load_forecast_record(FORECAST_HORIZON_TABLE, context),
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
        return db.fetch_all(
            f"""
            SELECT *
            FROM {MARKET_STRENGTH_TABLE}
            WHERE brand_key IN ({placeholders})
              AND source = %s AND market_id = %s AND view_kind = %s
            """,
            [*keys, context.source, context.market_id, context.view_kind],
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return []
        raise
    except pymysql.MySQLError:
        logger.warning("market-scoped brand strength lookup failed", exc_info=True)
        return []


def _load_forecast_record(
    table_name: str,
    context: DeepAnalysisContext,
) -> dict[str, Any] | None:
    if table_name not in {FORECAST_BLOCK_TABLE, FORECAST_HORIZON_TABLE}:
        raise ValueError(f"unsupported forecast serving table: {table_name}")
    try:
        return db.fetch_one(
            f"""
            SELECT *
            FROM {table_name}
            WHERE brand_key = %s AND source = %s AND market_id = %s AND view_kind = %s
            LIMIT 1
            """,
            [context.brand_key, context.source, context.market_id, context.view_kind],
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
        raise
    except pymysql.MySQLError:
        logger.warning("deep forecast serving lookup failed: table=%s", table_name, exc_info=True)
        return None
