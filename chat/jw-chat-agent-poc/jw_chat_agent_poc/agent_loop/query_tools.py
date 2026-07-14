from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from jw_chat_agent_poc.tools.query_layer import QueryCatalog, StrategicQueryLayer


BRAND_TOOLS = {
    "get_metric",
    "get_market_scope",
    "search_news",
    "get_disease_stats",
    "get_procedure_stats",
    "search_clinical",
    "search_drug_info",
    "web_search",
    "csd_activity_trend",
    "get_brand_sales",
    "get_brand_share",
    "get_brand_series",
    "compare_brands_series",
    "get_top_brands",
    "get_brand_channel_breakdown",
    "get_brand_specialty_breakdown",
}
PERIOD_TOOLS = {"get_metric", "get_brand_sales", "get_brand_share", "get_brand_series"}


@dataclass(frozen=True, slots=True)
class QueryToolResult:
    preview: str
    call: dict[str, Any]


def catalog_for(layer: StrategicQueryLayer | None, brand: str | None, market: str | None = None) -> QueryCatalog | None:
    if layer is None:
        return None
    try:
        return layer.catalog_for_brand(brand, market=market)
    except (LookupError, TypeError, ValueError):
        return None


def brand_metric(
    layer: StrategicQueryLayer | None,
    brand: str,
    metric: str,
    period: str,
    market: str | None = None,
    source: str = "",
    history_points: int = 10,
) -> QueryToolResult:
    active_layer = required_layer(layer)
    call = active_layer.brand_metric(
        brand,
        metric,
        period,
        market=market,
        source=source,
        history_points=history_points,
    )
    return QueryToolResult(f"{brand} {metric} query-layer", call)


def compare_series(layer: StrategicQueryLayer | None, brand: str, comparison: str, market: str | None = None) -> QueryToolResult:
    active_layer = required_layer(layer)
    if not comparison:
        raise LookupError("comparison_brand argument is required")
    call = active_layer.market_member_metric(brand, comparison, market=market)
    return QueryToolResult(f"{brand} vs {comparison} series query-layer", call)


def top_brands(
    layer: StrategicQueryLayer | None,
    brand: str,
    limit: str | None,
    market: str | None = None,
    source: str = "",
) -> QueryToolResult:
    active_layer = required_layer(layer)
    call = active_layer.top_brands(brand, int_arg(limit, 5), market=market, source=source)
    return QueryToolResult(f"{brand} top brands query-layer", call)


def dimension_breakdown(
    layer: StrategicQueryLayer | None,
    brand: str,
    dimension: str,
    arguments: Mapping[str, str],
    market: str | None = None,
) -> QueryToolResult:
    active_layer = required_layer(layer)
    call = active_layer.dimension_breakdown(
        brand,
        dimension,
        source=arguments.get("source", ""),
        period=arguments.get("period", "latest"),
        limit=int_arg(arguments.get("limit"), 10),
        market=market,
    )
    return QueryToolResult(f"{brand} {dimension} breakdown query-layer", call)


def query_spec(layer: StrategicQueryLayer | None, arguments: Mapping[str, str], fallback_brand: str) -> QueryToolResult:
    active_layer = required_layer(layer)
    call = active_layer.query(arguments.get("spec", {}), fallback_brand=fallback_brand)
    return QueryToolResult("query(spec) strategic mart", call)


def required_layer(layer: StrategicQueryLayer | None) -> StrategicQueryLayer:
    if layer is None:
        raise LookupError("query layer is unavailable")
    return layer


def int_arg(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
