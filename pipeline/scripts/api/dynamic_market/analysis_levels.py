"""Runtime analysis-level sections for dynamic cause payloads."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from typing import Any

from pymysql.err import MySQLError

from pipeline.scripts.api.dynamic_market.analysis_level_dimensions import build_analysis_rows
from pipeline.scripts.api.dynamic_market.cause_time import SOURCE_LABELS
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition


ETL_DIR = Path(__file__).resolve().parents[2] / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from pipeline.scripts.etl import build_cache_cause as cause_builder


logger = logging.getLogger(__name__)


def build_analysis_level_sections(
    *,
    definition: MarketDefinition,
    metrics: AggregatedMetrics,
    focus: BrandMetric | None,
    mart_db: str,
) -> dict[str, Any] | None:
    """Build the three cache-compatible analysis-level sections for dynamic data.

    The dynamic path deliberately reuses the deployed cache-cause builders.  It
    only adapts already-resolved mart rows into the row shape those builders
    expect; level selection remains data-driven from the focus brand's
    ``ml_market`` catalog row.
    """

    market = definition.market_catalog_row
    if not market:
        return None
    try:
        rows = build_analysis_rows(
            definition=definition,
            metrics=metrics,
            focus=focus,
            mart_db=mart_db,
        )
    except (MySQLError, RuntimeError, TypeError, ValueError, OSError):
        logger.warning("dynamic_analysis_level_dimension_rows_failed", exc_info=True)
        return None
    if not rows:
        return None
    source_api = SOURCE_LABELS.get(metrics.source, metrics.source.upper())
    view_source_id = _view_source_id(definition, market)
    channels = list(cause_builder._channels_for_source(source_api))
    try:
        analysis_levels = cause_builder._build_analysis_levels_from_mart(
            rows=rows,
            source=source_api,
            market=dict(market),
            view_source_id=view_source_id,
            target_name=None,
            fallback_level_top5={},
            channels_override=channels,
        )
        analysis_levels = cause_builder._ensure_split_class_alias(analysis_levels)
        rows_by_level = cause_builder._level_rows_by_segment(
            rows,
            analysis_levels.get("levels") or [],
        )
        level_top5_trend = cause_builder._level_top5_trend(
            analysis_levels,
            rows,
            source_api,
            focus.brand_name if focus else None,
            rows_by_level=rows_by_level,
            include_all_options=bool(focus),
            channel="전체",
        )
        market_status = cause_builder._ensure_analysis_level_market_status_contract(
            analysis_levels
        )
    except (KeyError, RuntimeError, TypeError, ValueError):
        logger.warning("dynamic_analysis_level_fill_failed", exc_info=True)
        return None
    return {
        "analysis_levels": analysis_levels,
        "analysis_level_market_status": market_status,
        "level_top5_trend": level_top5_trend,
    }


def _view_source_id(definition: MarketDefinition, market: dict[str, Any]) -> str | None:
    if definition.strategic_market_id:
        return definition.strategic_market_id
    value = market.get("ml_id") or market.get("cd_id") or market.get("cd_market_id")
    return str(value) if value not in (None, "") else None
