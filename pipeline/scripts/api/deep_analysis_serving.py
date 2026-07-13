"""Read-only adapters for the formal deep-analysis serving tables."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final

import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContext
from pipeline.scripts.api.deep_analysis_vocabulary import STRENGTH_VIEW_KIND_BY_FORMAL_VIEW


logger = logging.getLogger(__name__)

FORECAST_BLOCK_TABLE: Final = "deep_forecast_block"
MARKET_STRENGTH_TABLE: Final = "agent3_brand_strength_market"
SOURCE_STRENGTH_TABLE: Final = "agent3_brand_strength_source"


class ForecastBlockInvariantError(RuntimeError):
    """Raised when a serving block contradicts its availability marker."""


@dataclass(frozen=True, slots=True)
class ForecastBlock:
    forecast: object
    simulation: object
    generation_status: str | None
    no_history_fallback: object | None


def load_forecast_block(context: DeepAnalysisContext) -> ForecastBlock | None:
    """Load and validate the canonical block for one formal context."""

    return load_forecast_block_by_key(
        brand_key=context.brand_key,
        source=context.db_source,
        market_id=context.market_id,
    )


def load_forecast_block_by_key(
    *,
    brand_key: str,
    source: str,
    market_id: str,
) -> ForecastBlock | None:
    row = _fetch_forecast_block(brand_key=brand_key, source=source, market_id=market_id)
    return parse_forecast_block(row) if row else None


def parse_forecast_block(row: dict[str, Any]) -> ForecastBlock:
    decoded_simulation = _decode_json_section(row.get("simulation_json"))
    simulation_present = decoded_simulation is not None
    simulation_available = _coerce_bool(row.get("simulation_available"))
    if simulation_available != simulation_present:
        raise ForecastBlockInvariantError(
            "deep_forecast_block marker mismatch: "
            f"brand_key={row.get('brand_key')} source={row.get('source')} "
            f"market_id={row.get('market_id')} simulation_available={simulation_available} "
            f"simulation_json_present={simulation_present}"
        )

    status = _optional_text(row.get("generation_status"))
    fallback = _decode_json_section(row.get("no_history_fallback"))
    reason = "no_history" if _marks_no_history(status, fallback) else "not_generated"
    forecast = _decode_json_section(row.get("forecast_json"))
    if forecast is None:
        forecast = {"available": False, "reason": reason}
    simulation = (
        decoded_simulation
        if simulation_available
        else {"available": False, "reason": reason}
    )
    return ForecastBlock(
        forecast=forecast,
        simulation=simulation,
        generation_status=status,
        no_history_fallback=fallback,
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


def _fetch_forecast_block(
    *,
    brand_key: str,
    source: str,
    market_id: str,
) -> dict[str, Any] | None:
    """Fetch a block row without applying legacy horizon fallbacks."""

    try:
        return db.fetch_one(
            f"""
            SELECT * FROM {FORECAST_BLOCK_TABLE}
            WHERE brand_key = %s AND source = %s AND market_id = %s
            LIMIT 1
            """,
            [brand_key, source, market_id],
        )
    except pymysql.err.ProgrammingError as exc:
        if exc.args and exc.args[0] in {1054, 1146}:
            return None
        raise
    except pymysql.MySQLError:
        logger.warning("deep forecast serving lookup failed: table=%s", FORECAST_BLOCK_TABLE, exc_info=True)
        return None


def _decode_json_section(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, (dict, list)) else None


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _marks_no_history(status: str | None, fallback: object) -> bool:
    if status and "no_history" in status.lower():
        return True
    if isinstance(fallback, dict):
        reason = str(fallback.get("reason") or "").lower()
        if fallback.get("applied") is True and "history" in reason:
            return True
        return any(_marks_no_history(None, value) for value in fallback.values())
    if isinstance(fallback, list):
        return any(_marks_no_history(None, value) for value in fallback)
    return False
