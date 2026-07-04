from __future__ import annotations

from fastapi import APIRouter

from pipeline.scripts.api.config import config
from pipeline.scripts.api import db
from pipeline.scripts.api.openapi_docs import HEALTH_RESPONSES, META_TAG


router = APIRouter()


@router.get(
    "/api/health",
    tags=[META_TAG],
    summary="서비스 헬스체크",
    description="배포 버전과 cache 로드 개수를 반환합니다. 운영 전환 후 image tag, APP_VERSION, OpenAPI version 대조에 사용합니다.",
    response_model=None,
    responses=HEALTH_RESPONSES,
)
def health() -> dict:
    brands = db.fetch_one("SELECT JSON_LENGTH(response_json) AS c FROM cache_brands WHERE query_key='default'")
    market_status = db.fetch_one(
        "SELECT JSON_LENGTH(response_json, '$.brand_cards') AS c FROM cache_market_status WHERE query_key='default'"
    )
    return {
        "status": "ok",
        "markets_loaded": int(market_status["c"]) if market_status else 0,
        "brands_loaded": int(brands["c"]) if brands else 0,
        "version": config.app_version,
    }
