from __future__ import annotations

import time

from fastapi import APIRouter

from pipeline.scripts.api.cache.keys import cache_key_deep_analysis
from pipeline.scripts.api.cache.store import get_cache, set_cache
from pipeline.scripts.api.services import build_deep_analysis_response, latest_period_for_brand, resolve_brand


router = APIRouter()


@router.get("/api/deep-analysis/{brand_name}")
def deep_analysis(brand_name: str, period: str | None = None) -> dict:
    resolved = resolve_brand(brand_name)
    concrete_period = period or latest_period_for_brand(resolved.brand_id)
    key = cache_key_deep_analysis(resolved.display.brand_name, concrete_period)
    cached = get_cache(key)
    if cached:
        return cached
    start = time.perf_counter()
    response = build_deep_analysis_response(resolved.display.brand_name, concrete_period)
    set_cache(
        key,
        "deep_analysis",
        response,
        brand_name=resolved.display.brand_name,
        period_yyyymm=concrete_period,
        computation_ms=int((time.perf_counter() - start) * 1000),
    )
    return response
