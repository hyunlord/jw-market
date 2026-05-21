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


def _fetch_market_status(market_id: str, view: str, source: str, measure: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_market_status
        WHERE view_type = %s
          AND market_id = %s
          AND source = %s
          AND measure = %s
        LIMIT 1
        """,
        [view, market_id, source, measure],
    )
    return _load_response(row)


@router.get("/api/market-status/{market_id}")
def market_status(
    market_id: str,
    view: str = Query(..., pattern="^(general|strategic_ml|strategic_cd)$"),
    source: str = Query(..., pattern="^(ubist|iqvia|iqvia_nsa|UBIST|IQVIA|IQVIA_NSA)$"),
    measure: str = Query(...),
) -> dict:
    concrete_source = _normalise_source(source)
    response = _fetch_market_status(market_id, view, concrete_source, measure)
    if response is None:
        raise HTTPException(
            status_code=404,
            detail=f"Market-status cache not found for {market_id} / {view} / {concrete_source} / {measure}",
        )
    return response


@router.get("/api/market-status")
def market_status_query(
    market_id: str = Query(..., description="market id"),
    view: str = Query(..., pattern="^(general|strategic_ml|strategic_cd)$"),
    source: str = Query(..., pattern="^(ubist|iqvia|iqvia_nsa|UBIST|IQVIA|IQVIA_NSA)$"),
    measure: str = Query(...),
) -> dict:
    return market_status(market_id=market_id, view=view, source=source, measure=measure)
