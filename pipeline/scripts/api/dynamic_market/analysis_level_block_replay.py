"""Read precomputed analysis-level sections with mart-direct fallback semantics."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import threading
from typing import Any

from pymysql.err import MySQLError

from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
)


logger = logging.getLogger(__name__)
_REPLAY_STATS_LOCK = threading.Lock()
_REPLAY_STATS = {"hit": 0, "miss": 0, "fallback": 0}


def _record_replay_outcome(outcome: str) -> None:
    with _REPLAY_STATS_LOCK:
        _REPLAY_STATS[outcome] += 1
        outcome_count = _REPLAY_STATS[outcome]
        hits = _REPLAY_STATS["hit"]
        misses = _REPLAY_STATS["miss"]
        fallbacks = _REPLAY_STATS["fallback"]
        total = hits + misses + fallbacks
    if outcome_count == 1 or total % 100 == 0:
        logger.info(
            "analysis_level_block_replay_stats hits=%d misses=%d fallbacks=%d hit_rate=%.4f",
            hits,
            misses,
            fallbacks,
            hits / total,
        )


def reset_analysis_level_replay_stats_for_test() -> None:
    with _REPLAY_STATS_LOCK:
        for outcome in _REPLAY_STATS:
            _REPLAY_STATS[outcome] = 0


@dataclass(frozen=True, slots=True)
class AnalysisLevelBlockKey:
    view: str
    market_id: str
    source: str
    measure: str
    profile_sig: str = ""
    trim_mode: str = "full"


@dataclass(frozen=True, slots=True)
class AnalysisLevelBlock:
    analysis_levels: dict[str, Any]
    analysis_level_market_status: dict[str, Any]


def current_analysis_level_source_epoch() -> str | None:
    try:
        from pipeline.scripts.api.dynamic_market.runtime_cache import dynamic_response_cache

        return dynamic_response_cache._store.source_epoch()
    except (AttributeError, MySQLError, RuntimeError):
        logger.warning("analysis_level_block_epoch_fallback", exc_info=True)
        return None


def load_analysis_level_block(
    *,
    key: AnalysisLevelBlockKey,
    source_epoch: str,
) -> AnalysisLevelBlock | None:
    if os.environ.get("ANALYSIS_LEVEL_BLOCK_REPLAY_DISABLED") == "1":
        return None
    try:
        row = db.fetch_one(
            """
            SELECT analysis_levels_json, analysis_level_market_status_json
            FROM mart_analysis_level_block
            WHERE view = %s
              AND market_id = %s
              AND source = %s
              AND measure = %s
              AND profile_sig = %s
              AND trim_mode = %s
              AND build_version = %s
              AND source_epoch = %s
            LIMIT 1
            """,
            (
                key.view,
                key.market_id,
                key.source,
                key.measure,
                key.profile_sig,
                key.trim_mode,
                ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
                source_epoch,
            ),
        )
        if not row:
            _record_replay_outcome("miss")
            return None
        levels = json.loads(str(row["analysis_levels_json"]))
        status = json.loads(str(row["analysis_level_market_status_json"]))
        if not isinstance(levels, dict) or not isinstance(status, dict):
            _record_replay_outcome("fallback")
            return None
        _record_replay_outcome("hit")
        logger.info("analysis_level_block_hit key=%s", key)
        return AnalysisLevelBlock(levels, status)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, MySQLError):
        _record_replay_outcome("fallback")
        logger.warning("analysis_level_block_fallback key=%s", key, exc_info=True)
        return None
