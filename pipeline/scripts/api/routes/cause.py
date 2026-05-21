from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.utils import loads_json_maybe
from pipeline.scripts.api_response_builder.compose import compose_cause_response


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


def _fetch_cause_cache(
    brand_name: str,
    view: str,
    source: str,
    measure: str,
    market_id: str | None,
) -> dict[str, Any] | None:
    sql = """
        SELECT response_json
        FROM cache_cause
        WHERE view_type = %s
          AND source = %s
          AND measure = %s
          AND (brand_key = %s OR brand_name = %s)
    """
    params: list[Any] = [view, source, measure, brand_name, brand_name]
    if market_id:
        sql += " AND market_id = %s"
        params.append(market_id)
    sql += """
        ORDER BY CASE WHEN brand_key = %s THEN 0 ELSE 1 END, market_id
        LIMIT 1
    """
    params.append(brand_name)
    return _load_response(db.fetch_one(sql, params))


def _fetch_market_cache(cause_response: dict[str, Any]) -> dict[str, Any] | None:
    market_id = cause_response.get("market_id")
    view = cause_response.get("view")
    source = cause_response.get("source")
    measure = cause_response.get("measure")
    if not all([market_id, view, source, measure]):
        return None

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


@router.get("/api/cause/{brand_name}")
def cause(
    brand_name: str,
    view: str = Query(..., pattern="^(general|strategic_ml|strategic_cd)$"),
    source: str = Query(..., pattern="^(ubist|iqvia|iqvia_nsa|UBIST|IQVIA|IQVIA_NSA)$"),
    measure: str = Query(...),
    market_id: str | None = Query(None),
) -> dict:
    concrete_source = _normalise_source(source)
    cause_response = _fetch_cause_cache(brand_name, view, concrete_source, measure, market_id)
    if cause_response is None:
        raise HTTPException(
            status_code=404,
            detail=f"Cause cache not found for {brand_name} / {view} / {concrete_source} / {measure}",
        )

    market_response = _fetch_market_cache(cause_response)
    return compose_cause_response(cause_response, market_response)
