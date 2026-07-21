from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.openapi_docs import MARKET_STATUS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.api.services import market_recent_periods


router = APIRouter()


@router.get(
    "/api/market-status",
    tags=[PORTAL_CORE_TAG],
    summary="포탈 시장 현황 카드",
    description="운영 포탈 첫 화면의 시장 카드/상태 목록을 cache_market_status에서 그대로 반환합니다.",
    response_model=None,
    responses=MARKET_STATUS_RESPONSES,
)
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
    # Serving-time baseline labels (mart latest period per source); the cached
    # payload is otherwise returned verbatim.
    payload.update(market_recent_periods())
    return payload
