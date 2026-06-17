from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json


router = APIRouter()


@router.get("/api/market-status")
def market_status() -> dict:
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_market_status
        WHERE query_key = 'default'
        LIMIT 1
        """
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "cache_not_found", "cache": "cache_market_status"})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_market_status"})
    return payload
