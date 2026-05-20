from __future__ import annotations

import time

from fastapi import APIRouter

from pipeline.scripts.api.cache.keys import cache_key_cause
from pipeline.scripts.api.cache.store import get_cache, set_cache
from pipeline.scripts.api.services import build_cause_response, latest_period_for_brand, resolve_brand


router = APIRouter()


@router.get("/api/cause/{brand_name}")
def cause(
    brand_name: str,
    view: str = "market_landscape",
    source: str | None = None,
    measure: str = "sales",
    period: str | None = None,
) -> dict:
    resolved = resolve_brand(brand_name)
    concrete_period = period or latest_period_for_brand(resolved.brand_id)
    concrete_source = source or resolved.display.default_source
    key = cache_key_cause(resolved.display.brand_name, view, concrete_source, measure, concrete_period)
    cached = get_cache(key)
    if cached:
        return cached
    start = time.perf_counter()
    response = build_cause_response(
        resolved.display.brand_name,
        view=view,
        source=concrete_source,
        measure=measure,
        period=concrete_period,
    )
    set_cache(
        key,
        "cause",
        response,
        brand_name=resolved.display.brand_name,
        period_yyyymm=concrete_period,
        view=view,
        source=concrete_source,
        measure=measure,
        computation_ms=int((time.perf_counter() - start) * 1000),
    )
    return response
