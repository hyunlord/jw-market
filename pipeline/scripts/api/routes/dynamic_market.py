"""Dynamic market route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.filter_options import build_filter_options
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver, StrategicViewResolver
from pipeline.scripts.api.dynamic_market.strategic_runtime import build_strategic_payload
from pipeline.scripts.api.dynamic_market.strategic_runtime_cache import build_cached_payload
from pipeline.scripts.api.dynamic_market.types import (
    DynamicMarketInputError,
    DynamicMarketScopeTooBroadError,
    MarketDefinition,
    PeriodRange,
    clamp_top_n,
)
from pipeline.scripts.api.models.dynamic_market import DynamicMarketFilters, DynamicMarketRequest
from pipeline.scripts.api.openapi_docs import (
    COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE,
    DYNAMIC_MARKET_REQUEST_EXAMPLE,
    DYNAMIC_MARKET_RESPONSES,
    DYNAMIC_MARKET_TAG,
    FILTER_OPTIONS_RESPONSES,
)


router = APIRouter()


@router.post(
    "/api/dynamic-market",
    tags=[DYNAMIC_MARKET_TAG],
    summary="동적 시장 원인분석 재계산",
    description=(
        "전략뷰 ml_id/cd_market_id 또는 일반뷰 ATC4/molecule 범위를 입력받아 cache 없이 실시간으로 "
        "원인분석 payload를 재계산합니다. 응답 result는 /api/cause와 같은 root/data 구조입니다. "
        "analysis_level의 각 차원은 차원 내 OR, 차원 간 AND로 적용됩니다. "
        "전략뷰는 market_landscape(ml_id)와 competitive_dynamics(cd_market_id)를 모두 지원합니다."
    ),
    response_model=None,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "market_landscape": {"summary": "전략 시장조망 ml_id", "value": DYNAMIC_MARKET_REQUEST_EXAMPLE},
                        "competitive_dynamics": {
                            "summary": "전략 경쟁구도 cd_market_id",
                            "value": COMPETITIVE_DYNAMICS_REQUEST_EXAMPLE,
                        },
                    }
                }
            }
        }
    },
    responses=DYNAMIC_MARKET_RESPONSES,
)
def dynamic_market(payload: DynamicMarketRequest) -> dict:
    """Compute a caller-defined general-view market with the ``/api/cause`` response contract."""

    if _is_strategic_request(payload):
        try:
            _reject_strategic_channel_axis(payload)
            return build_cached_payload(
                builder=build_strategic_payload,
                mart_db=config.db_name,
                ml_id=_resolve_catalog_ml_id(payload.filters),
                cd_market_id=payload.filters.cd_market_id,
                focus_brand_key=payload.filters.focus_brand_key,
                source=payload.source,
                measure=payload.measure,
                analysis_level=payload.filters.analysis_level,
            )
        except DynamicMarketInputError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_dynamic_market_request", "message": str(exc)}) from exc

    aggregator = MetricAggregator(mart_db=config.db_name, strategic_dimension_db=config.strategic_dimension_db_name)
    composer = ResponseComposer()
    period_range = PeriodRange(
        start=payload.options.period_range.start if payload.options.period_range else None,
        end=payload.options.period_range.end if payload.options.period_range else None,
    )
    try:
        definition = _resolve_definition(payload)
        _enforce_scope_size_limit(definition, limit=config.dynamic_max_brand_rows)
        metrics = aggregator.aggregate(
            brands=definition.brands,
            source=definition.source,
            measure=definition.measure,
            period_range=period_range,
            top_n=clamp_top_n(payload.options.top_n),
            dimension_filters=definition.dimension_filters,
            channel_axis=definition.channel_axis,
            view=definition.view,
            strategic_market_id=definition.strategic_market_id,
        )
    except DynamicMarketScopeTooBroadError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "dynamic_scope_too_broad",
                "message": str(exc),
                "resolved_brand_rows": exc.resolved_brand_rows,
                "limit": exc.limit,
            },
        ) from exc
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_dynamic_market_request", "message": str(exc)}) from exc
    result = composer.compose(definition=definition, metrics=metrics)
    return compose_cached_json({"status": "SUCCESS", "result": result}, measure=payload.measure)


def _is_strategic_request(payload: DynamicMarketRequest) -> bool:
    filters = payload.filters
    return bool(filters.view_kind or filters.ml_id or filters.cd_market_id)


def _reject_strategic_channel_axis(payload: DynamicMarketRequest) -> None:
    try:
        channel_axis = payload.filters.channel_axis.to_filter(source=payload.source)
    except ValueError as exc:
        raise DynamicMarketInputError(str(exc)) from exc
    if channel_axis is not None and channel_axis.is_active:
        raise DynamicMarketInputError("channel_axis is supported only for general views")


def _enforce_scope_size_limit(definition: MarketDefinition, *, limit: int) -> None:
    resolved_brand_rows = len(definition.brands)
    if resolved_brand_rows > limit:
        raise DynamicMarketScopeTooBroadError(resolved_brand_rows=resolved_brand_rows, limit=limit)


def _resolve_definition(payload: DynamicMarketRequest):
    filters = payload.filters
    analysis_level = filters.analysis_level.model_dump()
    try:
        channel_axis = filters.channel_axis.to_filter(source=payload.source)
    except ValueError as exc:
        raise DynamicMarketInputError(str(exc)) from exc
    resolved_ml_id = _resolve_catalog_ml_id(filters)
    if filters.view_kind or filters.ml_id or filters.cd_market_id:
        return StrategicViewResolver(mart_db=config.db_name, dimension_db=config.strategic_dimension_db_name).resolve(
            view_kind=filters.view_kind,
            ml_id=resolved_ml_id,
            cd_market_id=filters.cd_market_id,
            atc4=filters.atc4,
            molecule=filters.molecule,
            analysis_level=analysis_level,
            channel_axis=channel_axis,
            focus_brand_key=filters.focus_brand_key,
            source=payload.source,
            measure=payload.measure,
        )
    return GeneralViewResolver(mart_db=config.db_name, bridge_db=config.bridge_db_name).resolve(
        atc4=filters.atc4,
        molecule=filters.molecule,
        analysis_level=analysis_level,
        channel_axis=channel_axis,
        focus_brand_key=filters.focus_brand_key,
        source=payload.source,
        measure=payload.measure,
    )


def _resolve_catalog_ml_id(filters: DynamicMarketFilters) -> str | None:
    """Resolve strategic ML markets from the focus brand when one is present.

    ``ml_id`` remains a compatibility/fallback input for brandless callers, but
    the strategy market definition is anchored by the focus brand's single
    catalog ML market.
    """

    if filters.cd_market_id or not filters.focus_brand_key:
        return filters.ml_id
    view_kind = (filters.view_kind or "").strip().lower()
    if view_kind not in {"market_landscape", "strategic_ml", "ml"}:
        return filters.ml_id
    display_brand = get_display_brand(filters.focus_brand_key.strip())
    if display_brand is None:
        return filters.ml_id
    return display_brand.ml_id


@router.get(
    "/api/dynamic-market/filter-options",
    tags=[DYNAMIC_MARKET_TAG],
    summary="동적 시장 필터 옵션",
    description=(
        "포탈 필터 UI가 사용하는 옵션 목록입니다. 전략뷰는 시장 소속 ATC/차원을 한 번에 반환하고, "
        "일반뷰는 선택된 ATC4 set 기준으로 소스별 scoped 옵션을 실시간 산출합니다."
    ),
    response_model=None,
    responses=FILTER_OPTIONS_RESPONSES,
)
def dynamic_market_filter_options(
    view: str = Query("general", description="[입력] general 또는 strategic.", examples=["general"]),
    source: str = Query("ubist", description="[입력] ubist 또는 iqvia.", examples=["ubist"]),
    measure: str = Query("sales", description="[입력] sales 또는 qty.", examples=["sales"]),
    brand: str | None = Query(
        default=None,
        description="[입력] 선택 브랜드명. market_id는 이 브랜드로 내부 조회되어 응답에 echo됩니다.",
        examples=["리바로"],
    ),
    atc4_codes: list[str] | None = Query(
        default=None,
        description="[입력] 일반뷰 2단계에서 선택된 ATC4 코드 목록. 여러 값을 보내면 OR 범위로 옵션을 재산출합니다.",
    ),
    selections: str | None = Query(
        default=None,
        description="[입력] 이미 선택된 차원 필터 JSON. 차원 내 OR, 차원 간 AND로 남은 옵션을 좁힙니다.",
    ),
    market_id: str | None = Query(default=None, include_in_schema=False, deprecated=True),
) -> dict:
    """Return dynamic filter options using brand-based market resolution.

    New portal callers should send only ``brand``, ``view``, and ``source``.
    ``market_id`` remains accepted as a hidden compatibility override for old
    scripts, but it is not part of the public Swagger contract.
    """

    try:
        return build_filter_options(
            mart_db=config.db_name,
            general_dimension_db=config.general_dimension_db_name,
            strategic_dimension_db=config.strategic_dimension_db_name,
            brand=brand,
            view=view,
            source=source,
            measure=measure,
            market_id=market_id,
            atc4_codes=atc4_codes,
            selections=selections,
        )
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_dynamic_market_filter_options_request", "message": str(exc)}) from exc


@router.get("/api/dynamic-market/brand-option-check", include_in_schema=False, deprecated=True)
def dynamic_market_brand_option_check(brand: str, view: str = "general", source: str = "ubist", market_id: str | None = None) -> dict:
    """Return option values and the sidecar values already matched by a brand.

    This endpoint exists for the test2 portal filter panel.  It keeps the
    option list contract identical to ``filter-options`` and adds
    ``brand_matched`` as dimension-type -> list, because a brand can span
    multiple product-level values (for example several forms or strengths).
    """

    try:
        return build_filter_options(
            mart_db=config.db_name,
            general_dimension_db=config.general_dimension_db_name,
            strategic_dimension_db=config.strategic_dimension_db_name,
            brand=brand,
            view=view,
            source=source,
            market_id=market_id,
        )
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_dynamic_market_brand_option_check_request", "message": str(exc)}) from exc


def strategic_stub_for_smoke() -> dict:
    """Exercise the future strategic resolver contract without mounting it.

    Test and audit scripts use this function to prove that a strategic resolver
    can feed the same aggregator/composer path.  The public MVP route remains
    general-view only until the overlay/CD filter rules are productized.
    """

    resolver = StrategicViewResolver(mart_db=config.db_name, dimension_db=config.strategic_dimension_db_name)
    definition = resolver.resolve(
        view_kind="market_landscape",
        ml_id="ml_003",
        cd_market_id=None,
        atc4=[],
        molecule=[],
        analysis_level=None,
        focus_brand_key=None,
        source="ubist",
        measure="sales",
    )
    metrics = MetricAggregator(mart_db=config.db_name, strategic_dimension_db=config.strategic_dimension_db_name).aggregate(
        brands=definition.brands,
        source=definition.source,
        measure=definition.measure,
        period_range=PeriodRange(),
        top_n=20,
        view=definition.view,
        strategic_market_id=definition.strategic_market_id,
    )
    return ResponseComposer().compose(definition=definition, metrics=metrics)
