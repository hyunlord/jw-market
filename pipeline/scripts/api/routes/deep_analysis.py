from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json


router = APIRouter()


@router.get("/api/deep-analysis/{brand_name}")
def deep_analysis(brand_name: str) -> dict:
    brand = unquote(brand_name)
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_deep_analysis
        WHERE brand = %s
        LIMIT 1
        """,
        [brand],
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_deep_analysis"})
    return payload
