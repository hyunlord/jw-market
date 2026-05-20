from __future__ import annotations

import time

from fastapi import APIRouter, Query

from pipeline.scripts.api.cache.keys import cache_key_brands
from pipeline.scripts.api.cache.store import get_cache, set_cache
from pipeline.scripts.api.models.brand import BrandResponse
from pipeline.scripts.api.services import build_brands_response


router = APIRouter()


@router.get("/api/brands", response_model=list[BrandResponse])
def list_brands(
    q: str | None = Query(None, description="브랜드명 부분 일치 검색"),
    market_id: str | None = Query(None, description="strategy_NNN market id"),
) -> list[dict]:
    key = cache_key_brands()
    cacheable = q is None and market_id is None
    if cacheable:
        cached = get_cache(key)
        if cached is not None:
            return cached
    start = time.perf_counter()
    response = build_brands_response(q=q, market_id=market_id)
    if cacheable:
        set_cache(key, "brands", response, computation_ms=int((time.perf_counter() - start) * 1000))
    return response
