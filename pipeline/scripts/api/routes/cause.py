from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from pipeline.scripts.api import db
from pipeline.scripts.api.brand_presence import brand_exists, missing_brand_cache
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.resolvers import normalize_measure, normalize_source
from pipeline.scripts.api.dynamic_market.response_cache import DynamicMarketOverloadedError, PersistenceScheduler
from pipeline.scripts.api.dynamic_market.runtime_cache import dynamic_response_cache
from pipeline.scripts.api.dynamic_market.strategic_cause import get_strategic_payload
from pipeline.scripts.api.dynamic_market.types import quote_identifier
from pipeline.scripts.api.handlers.multi_market import choose_primary_market
from pipeline.scripts.api.market_definition_display import apply_cd_market_definition
from pipeline.scripts.api.market_id import to_ml_id, to_strategy_id
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters
from pipeline.scripts.api.openapi_docs import CAUSE_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.api.validators.query_params import UNIT_LABELS, validate_cause_query


router = APIRouter()
_missing_brand_cache = missing_brand_cache


def _brand_exists(brand: str) -> bool:
    return brand_exists(brand)


def _fetch_cause_rows(
    brand: str,
    view: str,
    source: str,
    measure: str,
    market_id: str | None = None,
    *,
    persistence_scheduler: PersistenceScheduler | None = None,
) -> list[dict]:
    mart_source = normalize_source(source)
    mart_measure = normalize_measure(mart_source, measure)
    if view == "competitive_dynamics":
        selections = db.fetch_all(
            f"""
            SELECT DISTINCT b.cd_market_id AS view_source_id, c.ml_id
            FROM {quote_identifier(config.db_name)}.mart_strategic_cd_brand_metric b
            LEFT JOIN {quote_identifier(config.db_name)}.catalog_cd_market c
              ON c.cd_id = b.cd_market_id
            WHERE (b.brand_key = %s OR b.brand_name = %s)
              AND b.source = %s AND b.measure = %s
            ORDER BY b.cd_market_id
            """,
            [brand, brand, mart_source, mart_measure],
        )
    else:
        selections = db.fetch_all(
            f"""
            SELECT DISTINCT ml_id AS view_source_id, ml_id
            FROM {quote_identifier(config.db_name)}.mart_strategic_ml_brand_metric
            WHERE (brand_key = %s OR brand_name = %s)
              AND source = %s AND measure = %s
            ORDER BY ml_id
            """,
            [brand, brand, mart_source, mart_measure],
        )

    requested_ml_id = to_ml_id(market_id) if market_id else None
    rows: list[dict] = []
    empty_analysis_level = DynamicMarketAnalysisLevelFilters()
    for selection in selections:
        view_source_id = str(selection["view_source_id"])
        parent_ml_id = str(selection.get("ml_id") or view_source_id)
        response_market_id = to_strategy_id(parent_ml_id)
        if requested_ml_id and parent_ml_id != requested_ml_id and view_source_id != market_id:
            continue
        payload = get_strategic_payload(
            cache=dynamic_response_cache,
            mart_db=config.db_name,
            ml_id=view_source_id if view == "market_landscape" else None,
            cd_market_id=view_source_id if view == "competitive_dynamics" else None,
            focus_brand_key=brand,
            source=source,
            measure=measure,
            analysis_level=empty_analysis_level,
            persistence_scheduler=persistence_scheduler,
        )
        rows.append({"market_id": response_market_id, "response_json": payload})
    return rows


@router.get(
    "/api/cause/{brand_name}",
    tags=[PORTAL_CORE_TAG],
    summary="운영 포탈 원인분석 조회",
    description=(
        "최신 mart에서 원인분석 payload를 조립하고 bounded on-demand 캐시를 통해 반환합니다. "
        "응답 data는 포탈 렌더링 계약인 23개 섹션 구조이며, markets root 메타로 대표 시장을 표시합니다."
    ),
    response_model=None,
    responses=CAUSE_RESPONSES,
)
def cause(
    brand_name: str,
    background_tasks: BackgroundTasks,
    view: str | None = Query("market_landscape", description="조회 뷰. market_landscape 또는 competitive_dynamics.", examples=["market_landscape"]),
    source: str | None = Query("UBIST", description="데이터 소스. UBIST 또는 IQVIA.", examples=["UBIST"]),
    measure: str | None = Query("sales", description="지표. sales 또는 qty.", examples=["sales"]),
    market_id: str | None = Query(None, description="선택 시장 id. strategy_006 또는 ml_006 형태를 허용합니다.", examples=["strategy_006"]),
) -> dict:
    view, source, measure = validate_cause_query(view, source, measure)
    brand = unquote(brand_name)
    requested_market_id = to_strategy_id(market_id) if market_id else None
    if _missing_brand_cache.contains(brand):
        raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
    try:
        rows = _fetch_cause_rows(
            brand,
            view,
            source,
            measure,
            requested_market_id,
            persistence_scheduler=background_tasks.add_task,
        )
    except DynamicMarketOverloadedError as exc:
        raise HTTPException(
            status_code=429,
            detail={"error": "dynamic_market_overloaded", "message": str(exc)},
            headers={"Retry-After": "2"},
        ) from exc
    if not rows:
        if not _brand_exists(brand):
            _missing_brand_cache.remember(brand)
            raise HTTPException(status_code=404, detail={"error": "brand_not_found", "brand": brand})
        _missing_brand_cache.discard(brand)
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

    _missing_brand_cache.discard(brand)

    display_brand = get_display_brand(brand)
    preferred_market_id = requested_market_id or (display_brand.market_id if display_brand else None)
    primary, markets = choose_primary_market(rows, preferred_market_id=preferred_market_id)
    payload = primary["response_json"]
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_strategic_payload"})
    payload["markets"] = markets
    apply_cd_market_definition(payload, preserve_existing_actual_atcs=True)
    return payload
