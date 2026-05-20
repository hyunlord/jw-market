from __future__ import annotations

from fastapi import APIRouter

from pipeline.scripts.api import db
from pipeline.scripts.api.cache.store import count_cache_keys
from pipeline.scripts.api.utils import now_iso


router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    row = db.fetch_one("SELECT COUNT(*) AS row_count FROM mart_core_brand_metric")
    return {
        "status": "ok",
        "service": "jw-market-analysis-api",
        "layer3": {"metrics": 15, "rows": int(row["row_count"]) if row else 0},
        "layer4": {"cache_keys": count_cache_keys()},
        "generated_at": now_iso(),
    }
