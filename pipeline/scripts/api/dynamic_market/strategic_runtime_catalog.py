"""Catalog and cache adapters for strategic dynamic-market overlays."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any


JsonRow = dict[str, Any]


@lru_cache(maxsize=4)
def ml_market_catalog(cause_builder: Any) -> Mapping[str, JsonRow]:
    try:
        frame = cause_builder.load_catalog("ml_market")
    except Exception:
        return {}
    return {str(row["ml_id"]): row.to_dict() for _, row in frame.iterrows() if row.get("ml_id") is not None}


@lru_cache(maxsize=4)
def cd_market_catalog(cause_builder: Any) -> Mapping[str, JsonRow]:
    try:
        frame = cause_builder.load_catalog("cd_market").rename(columns={"cd_id": "cd_market_id"})
    except Exception:
        return {}
    return {
        str(row["cd_market_id"]): row.to_dict()
        for _, row in frame.iterrows()
        if row.get("cd_market_id") is not None
    }


@lru_cache(maxsize=2)
def strategic_brand_catalog(cause_builder: Any) -> Any:
    try:
        return cause_builder.load_catalog("strategic_brand")
    except Exception:
        return None


def catalog_row(cause_builder: Any, market_kind: str, view_source_id: str) -> JsonRow:
    catalog = cd_market_catalog(cause_builder) if market_kind == "cd" else ml_market_catalog(cause_builder)
    return dict(catalog.get(view_source_id, {}))


def response_market_id(cause_builder: Any, market_kind: str, view_source_id: str) -> str:
    if market_kind == "cd":
        source_id = catalog_row(cause_builder, "cd", view_source_id).get("ml_id") or view_source_id
        return str(cause_builder.ml_to_strategy(source_id))
    return str(cause_builder.ml_to_strategy(view_source_id))


def market_sources(cause_builder: Any, market_catalog_row: Mapping[str, Any], source_api: str) -> list[str]:
    sources = cause_builder.source_list(market_catalog_row.get("data_source"))
    return sources or [source_api]


def clear_runtime_caches(cause_builder: Any) -> None:
    for cache_name in (
        "ANALYSIS_LEVELS_CACHE",
        "LEVEL_ROW_GROUPS_CACHE",
        "ANALYSIS_LEVELS_BY_CHANNEL_CACHE",
        "ANALYSIS_LEVEL_STATUS_CHANNEL_CACHE",
        "EI_META_CACHE",
        "TARGET_RANK_STATS_CACHE",
    ):
        cache = getattr(cause_builder, cache_name, None)
        if hasattr(cache, "clear"):
            cache.clear()
