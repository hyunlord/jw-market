"""Shared mart-direct strategic cause assembly and cache-key contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pipeline.scripts.api.dynamic_market.response_cache import (
    CachedResponse,
    DynamicResponseCache,
    DynamicResponseCacheUnavailable,
    PersistenceScheduler,
)
from pipeline.scripts.api.dynamic_market.strategic_runtime import build_strategic_payload
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters
from pipeline.scripts.api.dynamic_market.types import PeriodRange


def strategic_cache_request(
    *,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
    period_range: PeriodRange = PeriodRange(),
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "contract": "strategic-cause-core-v1",
        "view": "strategic_cd" if cd_market_id else "strategic_ml",
        "market_id": cd_market_id or ml_id,
        "focus_brand_key": focus_brand_key,
        "source": source.lower(),
        "measure": measure.lower(),
        "analysis_level": analysis_level.model_dump(mode="json", by_alias=True),
    }
    if period_range.start is not None or period_range.end is not None:
        request["period_range"] = {"start": period_range.start, "end": period_range.end}
    return request


def get_strategic_payload(
    *,
    cache: DynamicResponseCache,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
    period_range: PeriodRange = PeriodRange(),
    persistence_scheduler: PersistenceScheduler | None = None,
) -> dict[str, Any]:
    request = strategic_cache_request(
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
        period_range=period_range,
    )
    builder = _strategic_builder(
        mart_db=mart_db,
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
        period_range=period_range,
    )
    try:
        return cache.get_or_build(
            request,
            builder,
            persistence_scheduler=persistence_scheduler,
        )
    except DynamicResponseCacheUnavailable:
        return builder()


def get_strategic_response(
    *,
    cache: DynamicResponseCache,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
    period_range: PeriodRange = PeriodRange(),
    persistence_scheduler: PersistenceScheduler | None = None,
) -> CachedResponse:
    request = strategic_cache_request(
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
        period_range=period_range,
    )
    builder = _strategic_builder(
        mart_db=mart_db,
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
        period_range=period_range,
    )

    try:
        return cache.get_or_build_response(
            request,
            builder,
            persistence_scheduler=persistence_scheduler,
        )
    except DynamicResponseCacheUnavailable:
        return CachedResponse(payload=builder())


def _strategic_builder(
    *,
    mart_db: str,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
    period_range: PeriodRange,
) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        return build_strategic_payload(
            mart_db=mart_db,
            ml_id=ml_id,
            cd_market_id=cd_market_id,
            focus_brand_key=focus_brand_key,
            source=source,
            measure=measure,
            analysis_level=analysis_level,
            period_range=period_range,
        )

    return build
