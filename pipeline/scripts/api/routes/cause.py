from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.handlers.multi_market import choose_primary_market
from pipeline.scripts.api.market_id import to_strategy_id
from pipeline.scripts.api.openapi_docs import CAUSE_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.api.validators.query_params import UNIT_LABELS, validate_cause_query


router = APIRouter()


def _brand_exists(brand: str) -> bool:
    return bool(db.fetch_one("SELECT 1 FROM cache_cause WHERE brand = %s LIMIT 1", [brand]))


def _fetch_cause_rows(
    brand: str,
    view: str,
    source: str,
    measure: str,
    market_id: str | None = None,
) -> list[dict]:
    params = [brand, view, source, measure]
    market_filter = ""
    if market_id:
        market_filter = " AND market_id = %s"
        params.append(market_id)
    return db.fetch_all(
        f"""
        SELECT market_id, response_json
        FROM cache_cause
        WHERE brand = %s
          AND view_type = %s
          AND source = %s
          AND measure = %s
          {market_filter}
        ORDER BY market_id
        """,
        params,
    )


@router.get(
    "/api/cause/{brand_name}",
    tags=[PORTAL_CORE_TAG],
    summary="운영 포탈 원인분석 조회",
    description=(
        "cache_cause에 저장된 운영 원인분석 payload를 그대로 반환합니다. "
        "응답 data는 포탈 렌더링 계약인 23개 섹션 구조이며, markets root 메타로 대표 시장을 표시합니다."
    ),
    response_model=None,
    responses=CAUSE_RESPONSES,
)
def cause(
    brand_name: str,
    view: str | None = Query("market_landscape", description="조회 뷰. market_landscape 또는 competitive_dynamics.", examples=["market_landscape"]),
    source: str | None = Query("UBIST", description="데이터 소스. UBIST 또는 IQVIA.", examples=["UBIST"]),
    measure: str | None = Query("sales", description="지표. sales 또는 qty.", examples=["sales"]),
    market_id: str | None = Query(None, description="선택 시장 id. strategy_006 또는 ml_006 형태를 허용합니다.", examples=["strategy_006"]),
) -> dict:
    view, source, measure = validate_cause_query(view, source, measure)
    brand = unquote(brand_name)
    requested_market_id = to_strategy_id(market_id) if market_id else None
    rows = _fetch_cause_rows(brand, view, source, measure, requested_market_id)
    if not rows:
        if not _brand_exists(brand):
            raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
        return {
            "brand": brand,
            "market_id": requested_market_id,
            "view": view,
            "source": source,
            "measure": measure,
            "unit_label": UNIT_LABELS[(source, measure)],
            "data": None,
            "reason": "brand_not_in_source",
            "market_meta": None,
            "markets": [],
        }

    display_brand = get_display_brand(brand)
    preferred_market_id = requested_market_id or (display_brand.market_id if display_brand else None)
    primary, markets = choose_primary_market(rows, preferred_market_id=preferred_market_id)
    payload = compose_cached_json(primary["response_json"], measure=measure)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_cause"})
    payload["markets"] = markets
    return payload
