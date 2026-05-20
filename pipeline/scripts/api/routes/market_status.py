from __future__ import annotations

import time

from fastapi import APIRouter, Query

from pipeline.scripts.api.cache.keys import cache_key_market_status
from pipeline.scripts.api.cache.store import get_cache, set_cache
from pipeline.scripts.api.models.market_status import MarketStatusCard
from pipeline.scripts.api.services import build_market_status_response, filter_market_status_cards


router = APIRouter()


@router.get("/api/market-status", response_model=list[MarketStatusCard])
def market_status(
    market_id: str | None = Query(None, description="strategy_NNN market id"),
) -> list[dict]:
    key = cache_key_market_status("all")
    cached = get_cache(key)
    if cached is not None:
        return filter_market_status_cards(cached, market_id=market_id)

    start = time.perf_counter()
    response = build_market_status_response()
    set_cache(
        key,
        "market_status",
        response,
        computation_ms=int((time.perf_counter() - start) * 1000),
    )
    return filter_market_status_cards(response, market_id=market_id)
