from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.handlers.multi_market import choose_primary_market
from pipeline.scripts.api.validators.query_params import UNIT_LABELS, validate_cause_query


router = APIRouter()


def _brand_exists(brand: str) -> bool:
    return bool(db.fetch_one("SELECT 1 FROM cache_cause WHERE brand = %s LIMIT 1", [brand]))


def _fetch_cause_rows(brand: str, view: str, source: str, measure: str) -> list[dict]:
    return db.fetch_all(
        """
        SELECT market_id, response_json
        FROM cache_cause
        WHERE brand = %s
          AND view_type = %s
          AND source = %s
          AND measure = %s
        ORDER BY market_id
        """,
        [brand, view, source, measure],
    )


@router.get("/api/cause/{brand_name}")
def cause(
    brand_name: str,
    view: str | None = Query(None),
    source: str | None = Query(None),
    measure: str | None = Query(None),
) -> dict:
    view, source, measure = validate_cause_query(view, source, measure)
    brand = unquote(brand_name)
    rows = _fetch_cause_rows(brand, view, source, measure)
    if not rows:
        if not _brand_exists(brand):
            raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
        return {
            "brand": brand,
            "market_id": None,
            "view": view,
            "source": source,
            "measure": measure,
            "unit_label": UNIT_LABELS[(source, measure)],
            "data": None,
            "reason": "brand_not_in_source",
            "market_meta": None,
            "markets": [],
        }

    primary, markets = choose_primary_market(rows)
    payload = compose_cached_json(primary["response_json"], measure=measure)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_cause"})
    payload["markets"] = markets
    return payload
