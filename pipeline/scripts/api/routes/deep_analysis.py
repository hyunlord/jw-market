from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.utils import loads_json_maybe


router = APIRouter()


def _normalise_source(source: str) -> str:
    lowered = source.lower()
    if lowered == "iqvia":
        return "iqvia_nsa"
    return lowered


def _load_response(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    response = loads_json_maybe(row.get("response_json"))
    return response if isinstance(response, dict) else None


@router.get("/api/deep-analysis/{brand_name}")
def deep_analysis(
    brand_name: str,
    view: str = Query(..., pattern="^(general|strategic_ml|strategic_cd)$"),
    source: str = Query(..., pattern="^(ubist|iqvia|iqvia_nsa|UBIST|IQVIA|IQVIA_NSA)$"),
    measure: str = Query(...),
    market_id: str | None = Query(None),
) -> dict:
    concrete_source = _normalise_source(source)
    sql = """
        SELECT response_json
        FROM cache_deep_analysis
        WHERE view_type = %s
          AND source = %s
          AND measure = %s
          AND (brand_key = %s OR brand_name = %s)
    """
    params: list[Any] = [view, concrete_source, measure, brand_name, brand_name]
    if market_id:
        sql += " AND market_id = %s"
        params.append(market_id)
    sql += """
        ORDER BY CASE WHEN brand_key = %s THEN 0 ELSE 1 END, market_id
        LIMIT 1
    """
    params.append(brand_name)

    response = _load_response(db.fetch_one(sql, params))
    if response is None:
        raise HTTPException(
            status_code=404,
            detail=f"Deep-analysis cache not found for {brand_name} / {view} / {concrete_source} / {measure}",
        )
    return response
