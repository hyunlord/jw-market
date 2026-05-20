from __future__ import annotations

import time

from fastapi import APIRouter

from pipeline.scripts.api.cache.keys import cache_key_market_status
from pipeline.scripts.api.cache.store import get_cache, set_cache
from pipeline.scripts.api.services import build_market_status_response, normalize_market_status_top_n


router = APIRouter()


@router.get("/api/market-status")
def market_status(period: str | None = None, top_n: int = 20) -> dict:
    top_n = normalize_market_status_top_n(top_n)
    key_period = period or "latest"
    key = cache_key_market_status(key_period, top_n=top_n)
    cached = get_cache(key)
    if cached:
        return cached
    start = time.perf_counter()
    response = build_market_status_response(key_period, top_n=top_n)
    set_cache(
        key,
        "market_status",
        response,
        period_yyyymm=key_period,
        computation_ms=int((time.perf_counter() - start) * 1000),
    )
    return response
