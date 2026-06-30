"""Small in-process cache for strategic dynamic-market runtime payloads."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
import json
from threading import RLock
from typing import Any

from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters


JsonRow = dict[str, Any]
PayloadBuilder = Callable[..., JsonRow]

_CACHE_LOCK = RLock()
_CACHE_MAX = 64
_CACHE: OrderedDict[tuple[str, ...], JsonRow] = OrderedDict()


def build_cached_payload(
    *,
    builder: PayloadBuilder,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> JsonRow:
    cache_key = _payload_cache_key(
        mart_db=mart_db,
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
    )
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            _CACHE.move_to_end(cache_key)
            return deepcopy(cached)
    composed = builder(
        mart_db=mart_db,
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
    )
    with _CACHE_LOCK:
        _CACHE[cache_key] = deepcopy(composed)
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return composed


def _payload_cache_key(
    *,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> tuple[str, ...]:
    analysis_json = json.dumps(analysis_level.model_dump(), ensure_ascii=False, sort_keys=True)
    return (
        mart_db,
        ml_id or "",
        cd_market_id or "",
        focus_brand_key or "",
        source,
        measure,
        analysis_json,
    )
