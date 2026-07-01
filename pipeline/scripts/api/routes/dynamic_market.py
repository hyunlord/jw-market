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
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, PeriodRange, clamp_top_n
from pipeline.scripts.api.models.dynamic_market import DynamicMarketFilters, DynamicMarketRequest


router = APIRouter()


@router.post("/api/dynamic-market")
def dynamic_market(payload: DynamicMarketRequest) -> dict:
    """Compute a caller-defined general-view market with the ``/api/cause`` response contract."""

    resolved_ml_id = _resolve_catalog_ml_id(payload.filters)
    if resolved_ml_id or payload.filters.cd_market_id:
        try:
            result = build_strategic_payload(
                mart_db=config.db_name,
                ml_id=resolved_ml_id,
                cd_market_id=payload.filters.cd_market_id,
                focus_brand_key=payload.filters.focus_brand_key,
                source=payload.source,
                measure=payload.measure,
                analysis_level=payload.filters.analysis_level,
            )
        except DynamicMarketInputError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_dynamic_market_request", "message": str(exc)},
            ) from exc
        return {"status": "SUCCESS", "result": result}

    aggregator = MetricAggregator(mart_db=config.db_name)
    composer = ResponseComposer()
    period_range = PeriodRange(
        start=payload.options.period_range.start if payload.options.period_range else None,
        end=payload.options.period_range.end if payload.options.period_range else None,
    )
    try:
        definition = _resolve_definition(payload)
        metrics = aggregator.aggregate(
            brands=definition.brands,
            source=definition.source,
            measure=definition.measure,
            period_range=period_range,
            top_n=clamp_top_n(payload.options.top_n),
            dimension_filters=definition.dimension_filters,
            view=definition.view,
            strategic_market_id=definition.strategic_market_id,
        )
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_dynamic_market_request", "message": str(exc)}) from exc
    result = composer.compose(definition=definition, metrics=metrics)
    return compose_cached_json({"status": "SUCCESS", "result": result}, measure=payload.measure)


def _resolve_definition(payload: DynamicMarketRequest):
    filters = payload.filters
    analysis_level = filters.analysis_level.model_dump()
    resolved_ml_id = _resolve_catalog_ml_id(filters)
    if filters.view_kind or filters.ml_id or filters.cd_market_id:
        return StrategicViewResolver(mart_db=config.db_name, dimension_db=config.strategic_dimension_db_name).resolve(
            view_kind=filters.view_kind,
            ml_id=resolved_ml_id,
            cd_market_id=filters.cd_market_id,
            atc4=filters.atc4,
            molecule=filters.molecule,
            analysis_level=analysis_level,
            focus_brand_key=filters.focus_brand_key,
            source=payload.source,
            measure=payload.measure,
        )
    return GeneralViewResolver(mart_db=config.db_name, bridge_db=config.bridge_db_name).resolve(
        atc4=filters.atc4,
        molecule=filters.molecule,
        analysis_level=analysis_level,
        focus_brand_key=filters.focus_brand_key,
        source=payload.source,
        measure=payload.measure,
    )


def _resolve_catalog_ml_id(filters: DynamicMarketFilters) -> str | None:
    """Resolve strategic ML markets from the catalog only when callers omit ``ml_id``.

    Existing explicit market ids stay authoritative.  Competitive-dynamics
    requests are intentionally not inferred because the display catalog carries
    the strategic ML id, not a CD market id.
    """

    if filters.ml_id or filters.cd_market_id or not filters.focus_brand_key:
        return filters.ml_id
    view_kind = (filters.view_kind or "").strip().lower()
    if view_kind not in {"market_landscape", "strategic_ml", "ml"}:
        return filters.ml_id
    display_brand = get_display_brand(filters.focus_brand_key.strip())
    if display_brand is None:
        return filters.ml_id
    return display_brand.ml_id


@router.get("/api/dynamic-market/filter-options")
def dynamic_market_filter_options(
    view: str = "general",
    source: str = "ubist",
    brand: str | None = Query(
        default=None,
        description="[입력] 선택 브랜드명. market_id는 이 브랜드로 내부 조회되어 응답에 echo됩니다.",
        examples=["리바로"],
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
            market_id=market_id,
        )
    except DynamicMarketInputError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_dynamic_market_filter_options_request", "message": str(exc)}) from exc


@router.get("/api/dynamic-market/brand-option-check")
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
    metrics = MetricAggregator(mart_db=config.db_name).aggregate(
        brands=definition.brands,
        source=definition.source,
        measure=definition.measure,
        period_range=PeriodRange(),
        top_n=20,
    )
    return ResponseComposer().compose(definition=definition, metrics=metrics)
