from __future__ import annotations

import time

from fastapi import APIRouter

from pipeline.scripts.api.cache.keys import cache_key_brands
from pipeline.scripts.api.cache.store import get_cache, set_cache
from pipeline.scripts.api.services import build_brands_response


router = APIRouter()


@router.get("/api/brands")
def list_brands(include_snapshot: bool = False) -> dict:
    key = cache_key_brands()
    if not include_snapshot:
        cached = get_cache(key)
        if cached:
            return cached
    start = time.perf_counter()
    response = build_brands_response(include_snapshot=include_snapshot)
    if not include_snapshot:
        set_cache(key, "brands", response, computation_ms=int((time.perf_counter() - start) * 1000))
    return response
