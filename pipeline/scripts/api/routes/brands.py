from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.openapi_docs import BRANDS_RESPONSES, PORTAL_CORE_TAG


router = APIRouter()


def _default_brands() -> list[dict]:
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_brands
        WHERE query_key = 'default'
        LIMIT 1
        """
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "cache_not_found", "cache": "cache_brands"})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, list):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_brands"})
    return payload


@router.get(
    "/api/brands",
    tags=[PORTAL_CORE_TAG],
    summary="포탈 브랜드 목록",
    description="포탈 검색/선택에 사용하는 브랜드 catalog cache를 반환합니다. q와 market_id는 반환 목록만 필터링합니다.",
    response_model=None,
    responses=BRANDS_RESPONSES,
)
def list_brands(
    q: str | None = Query(None, description="브랜드명 부분 일치 검색어입니다.", examples=["리바로"]),
    market_id: str | None = Query(None, description="strategy_NNN 형식의 시장 id로 브랜드 목록을 제한합니다.", examples=["strategy_006"]),
) -> list[dict]:
    brands = _default_brands()
    if q:
        needle = q.casefold()
        brands = [brand for brand in brands if needle in str(brand.get("brand", "")).casefold()]
    if market_id:
        brands = [brand for brand in brands if brand.get("market_id") == market_id]
    return brands
