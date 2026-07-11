"""Shared mart-direct strategic cause assembly and cache-key contract."""

from __future__ import annotations

from typing import Any

from pipeline.scripts.api.dynamic_market.response_cache import (
    DynamicResponseCache,
    DynamicResponseCacheUnavailable,
)
from pipeline.scripts.api.dynamic_market.strategic_runtime import build_strategic_payload
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters


def strategic_cache_request(
    *,
    ml_id: str | None,
    cd_market_id: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
    analysis_level: DynamicMarketAnalysisLevelFilters,
) -> dict[str, Any]:
    return {
        "contract": "strategic-cause-core-v1",
        "view": "strategic_cd" if cd_market_id else "strategic_ml",
        "market_id": cd_market_id or ml_id,
        "focus_brand_key": focus_brand_key,
        "source": source.lower(),
        "measure": measure.lower(),
        "analysis_level": analysis_level.model_dump(mode="json", by_alias=True),
    }


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
) -> dict[str, Any]:
    request = strategic_cache_request(
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key=focus_brand_key,
        source=source,
        measure=measure,
        analysis_level=analysis_level,
    )

    def build() -> dict[str, Any]:
        return build_strategic_payload(
            mart_db=mart_db,
            ml_id=ml_id,
            cd_market_id=cd_market_id,
            focus_brand_key=focus_brand_key,
            source=source,
            measure=measure,
            analysis_level=analysis_level,
        )

    try:
        return cache.get_or_build(request, build)
    except DynamicResponseCacheUnavailable:
        return build()
