from __future__ import annotations

from fastapi import APIRouter

from pipeline.scripts.api.config import config
from pipeline.scripts.api import db


router = APIRouter()


@router.get("/api/health")
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
