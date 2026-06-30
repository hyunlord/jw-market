"""Dynamic market route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver, StrategicViewResolver
from pipeline.scripts.api.dynamic_market.strategic_runtime import build_strategic_payload
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, PeriodRange, clamp_top_n
from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest


router = APIRouter()


@router.post("/api/dynamic-market")
def dynamic_market(payload: DynamicMarketRequest) -> dict:
    """Compute a caller-defined general-view market with the ``/api/cause`` response contract."""

    if payload.filters.ml_id or payload.filters.cd_market_id:
        try:
            result = build_strategic_payload(
                mart_db=config.db_name,
                ml_id=payload.filters.ml_id,
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

    resolver = GeneralViewResolver(mart_db=config.db_name, bridge_db=config.bridge_db_name)
    aggregator = MetricAggregator(mart_db=config.db_name)
    composer = ResponseComposer()
    period_range = PeriodRange(
        start=payload.options.period_range.start if payload.options.period_range else None,
        end=payload.options.period_range.end if payload.options.period_range else None,
    )
    try:
        definition = resolver.resolve(
            atc4=payload.filters.atc4,
            molecule=payload.filters.molecule,
            source=payload.source,
            measure=payload.measure,
        )
        metrics = aggregator.aggregate(
            brands=definition.brands,
            source=definition.source,
            measure=definition.measure,
            period_range=period_range,
            top_n=clamp_top_n(payload.options.top_n),
        )
    except DynamicMarketInputError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_dynamic_market_request", "message": str(exc)},
        ) from exc
    return {"status": "SUCCESS", "result": composer.compose(definition=definition, metrics=metrics)}


def strategic_stub_for_smoke() -> dict:
    """Exercise the future strategic resolver contract without mounting it.

    Test and audit scripts use this function to prove that a strategic resolver
    can feed the same aggregator/composer path.  The public MVP route remains
    general-view only until the overlay/CD filter rules are productized.
    """

    resolver = StrategicViewResolver(mart_db=config.db_name)
    definition = resolver.resolve(atc4=[], molecule=[], source="ubist", measure="sales")
    metrics = MetricAggregator(mart_db=config.db_name).aggregate(
        brands=definition.brands,
        source=definition.source,
        measure=definition.measure,
        period_range=PeriodRange(),
        top_n=20,
    )
    return ResponseComposer().compose(definition=definition, metrics=metrics)
