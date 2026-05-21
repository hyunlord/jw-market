from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.utils import loads_json_maybe


router = APIRouter()


def _normalise_source(source: str | None) -> str:
    if not source:
        return "all"
    lowered = source.lower()
    if lowered == "iqvia":
        return "iqvia_nsa"
    return lowered


def _load_response(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    response = loads_json_maybe(row.get("response_json"))
    return response if isinstance(response, dict) else None


def _filter_brands(response: dict[str, Any], q: str | None, market_id: str | None) -> dict[str, Any]:
    if not q and not market_id:
        return response
    brands = response.get("brands")
    if not isinstance(brands, list):
        return response

    filtered = []
    needle = q.casefold() if q else None
    for brand in brands:
        if not isinstance(brand, dict):
            continue
        if needle and needle not in str(brand.get("brand_name") or brand.get("brand_key") or "").casefold():
            continue
        if market_id:
            market_ids = brand.get("market_ids") or brand.get("available_markets") or brand.get("atc4_codes") or []
            if market_id not in market_ids:
                continue
        filtered.append(brand)

    result = dict(response)
    result["brands"] = filtered
    result["total_count"] = len(filtered)
    result["filters_applied"] = {
        **(response.get("filters_applied") or {}),
        "q": q,
        "market_id": market_id,
    }
    return result


@router.get("/api/brands")
def list_brands(
    q: str | None = Query(None, description="브랜드명 부분 일치 검색"),
    market_id: str | None = Query(None, description="strategy_NNN market id"),
    view: str | None = Query(None, pattern="^(general|strategic_ml|strategic_cd)$"),
    source: str | None = Query(None, pattern="^(ubist|iqvia|iqvia_nsa|UBIST|IQVIA|IQVIA_NSA)$"),
) -> dict:
    view_key = view or "all"
    source_key = _normalise_source(source)

    response = _load_response(
        db.fetch_one(
            """
            SELECT response_json
            FROM cache_brands
            WHERE view_type = %s
              AND source = %s
            LIMIT 1
            """,
            [view_key, source_key],
        )
    )

    if response is None and view and source:
        response = _load_response(
            db.fetch_one(
                """
                SELECT response_json
                FROM cache_brands
                WHERE view_type = 'all'
                  AND source = %s
                LIMIT 1
                """,
                [source_key],
            )
        )

    if response is None:
        raise HTTPException(status_code=404, detail=f"Brands cache not found for view={view_key}, source={source_key}")

    return _filter_brands(response, q=q, market_id=market_id)
