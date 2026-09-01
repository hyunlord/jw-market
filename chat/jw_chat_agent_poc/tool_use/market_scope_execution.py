from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jw_chat_agent_poc.tool_use.market_scope_backends import (
    GeneralMarketBackend,
    StrategicMarketBackend,
)

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    AmbiguousFamilyError,
    AmbiguousMarketError,
    BrandOutsideCompositeScopeError,
    GeneralCompositeUnavailableError,
    GeneralMetricUnavailableError,
    InvalidMarketLabelError,
    MarketScope,
    MarketScopeKind,
    MarketScopeResolutionError,
    NoAnchorError,
    NoStrategicMembershipError,
    ScopeResolution,
    UnknownBrandError,
    UnsupportedSourceError,
)
from jw_chat_agent_poc.tool_use.market_scope_input import (
    integer,
    optional_text,
    required_text,
    text,
)
from jw_chat_agent_poc.tool_use.market_scope_projection import (
    assert_general_scope,
    attach_normalization_trace,
    general_comparison_result,
    general_metric_result,
    general_metric_value,
    general_scope_result,
    general_timeseries_result,
    rounded_hhi,
)
from jw_chat_agent_poc.tool_use.market_scope_resolver import ScopeResolver


__all__ = (
    "AmbiguousFamilyError",
    "AmbiguousMarketError",
    "BrandOutsideCompositeScopeError",
    "GeneralCompositeUnavailableError",
    "GeneralMetricUnavailableError",
    "InvalidMarketLabelError",
    "MarketScope",
    "MarketScopeCatalogBackend",
    "MarketScopeKind",
    "MarketScopeResolutionError",
    "NoAnchorError",
    "NoStrategicMembershipError",
    "ScopeResolution",
    "ScopeResolver",
    "UnknownBrandError",
    "UnsupportedSourceError",
)


class MarketScopeCatalogBackend:
    """Add a general ATC4 surface in V3 SHADOW while preserving strategic calls."""

    def __init__(
        self,
        strategic: StrategicMarketBackend,
        resolver: ScopeResolver,
        general: GeneralMarketBackend,
    ) -> None:
        self._strategic = strategic
        self._resolver = resolver
        self._general = general

    def execute_catalog_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, Any]:
        resolution = self._resolver.resolve(arguments)
        if resolution.scope.kind is MarketScopeKind.STRATEGIC:
            result = self._execute_strategic(name, resolution.normalized_arguments)
            return attach_normalization_trace(result, resolution)
        return self._execute_general(name, resolution)

    def brand_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        market: str | None = None,
        source: str = "",
        history_points: int = 10,
    ) -> dict[str, Any]:
        return self.execute_catalog_tool(
            "market.get_brand_metric",
            {
                "brand": brand,
                "metric": metric,
                "period": period,
                "market": market,
                "source": source,
                "history_points": history_points,
            },
        )

    def market_scope(self, brand: str, market: str | None = None) -> dict[str, Any]:
        return self.execute_catalog_tool(
            "market.get_market_size", {"brand": brand, "market": market}
        )

    def dimension_breakdown(
        self,
        brand: str,
        dimension: str,
        source: str = "",
        period: str = "latest",
        limit: int = 10,
        market: str | None = None,
        metric: str = "sales",
    ) -> dict[str, Any]:
        return self.execute_catalog_tool(
            "market.get_channel_breakdown",
            {
                "brand": brand,
                "source": source,
                "period": period,
                "limit": limit,
                "market": market,
                "metric": metric,
            },
        )

    def market_member_metric(
        self,
        brand: str,
        comparison: str,
        market: str | None = None,
        metric: str = "series",
    ) -> dict[str, Any]:
        return self.execute_catalog_tool(
            "market.compare_brands",
            {
                "brand": brand,
                "comparison_brand": comparison,
                "market": market,
                "metric": metric,
            },
        )

    def _execute_strategic(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> dict[str, Any]:
        brand = required_text(arguments, "brand")
        market = optional_text(arguments, "market")
        if name in {"market.get_brand_metric", "market.get_timeseries"}:
            return self._strategic.brand_metric(
                brand,
                text(arguments, "metric", "sales"),
                text(arguments, "period", "latest"),
                market=market,
                source=text(arguments, "source", ""),
                history_points=integer(arguments, "history_points", 10),
            )
        if name in {"market.get_market_size", "market.get_market_members"}:
            return self._strategic.market_scope(brand, market=market)
        if name == "market.get_channel_breakdown":
            return self._strategic.dimension_breakdown(
                brand,
                "channel",
                source=text(arguments, "source", ""),
                period=text(arguments, "period", "latest"),
                limit=integer(arguments, "limit", 10),
                market=market,
                metric=text(arguments, "metric", "sales"),
            )
        if name in {"market.get_hhi", "market.get_growth_contribution"}:
            metric = "hhi" if name.endswith("get_hhi") else "growth_contribution"
            return self._strategic.brand_metric(
                brand,
                metric,
                text(arguments, "period", "latest"),
                market=market,
                source=text(arguments, "source", ""),
                history_points=integer(arguments, "history_points", 10),
            )
        if name == "market.compare_brands":
            return self._strategic.market_member_metric(
                brand,
                required_text(arguments, "comparison_brand"),
                market=market,
                metric=text(arguments, "metric", "series"),
            )
        raise LookupError(f"unregistered market catalog tool: {name}")

    def _execute_general(
        self,
        name: str,
        resolution: ScopeResolution,
    ) -> dict[str, Any]:
        arguments = resolution.normalized_arguments
        brand = required_text(arguments, "brand")
        metric = text(arguments, "metric", "sales")
        measure = (
            "qty"
            if metric.casefold() in {"volume", "prescription_volume"}
            else "sales"
        )
        atc4 = resolution.scope.atc4[0]
        if resolution.scope.kind is MarketScopeKind.GENERAL_COMPOSITE:
            market = self._general.composite_market(
                resolution.scope.atc4,
                resolution.scope.filters,
                brand,
                resolution.source,
                measure,
            )
        else:
            market = self._general.market(atc4, brand, resolution.source, measure)
        assert_general_scope(
            market,
            resolution.scope.atc4,
            brand,
            filters=resolution.scope.filters,
        )
        if name in {"market.get_market_size", "market.get_market_members"}:
            return general_scope_result(name, market, resolution)
        if name == "market.get_hhi":
            return general_metric_result(
                market, "hhi", rounded_hhi(market.hhi_recent), resolution
            )
        if name == "market.get_growth_contribution":
            return general_metric_result(
                market,
                "growth_contribution",
                general_metric_value(market, "growth_contribution"),
                resolution,
            )
        if name == "market.get_channel_breakdown":
            if resolution.scope.kind is not MarketScopeKind.GENERAL_COMPOSITE:
                raise GeneralMetricUnavailableError(
                    "general ATC4 channel breakdown requires composite execution"
                )
            return general_metric_result(
                market, "channel_breakdown", market.dashboard_tables, resolution
            )
        if name == "market.get_timeseries":
            return general_timeseries_result(market, metric, resolution)
        if name == "market.compare_brands":
            comparison = required_text(arguments, "comparison_brand")
            if resolution.scope.kind is MarketScopeKind.GENERAL_COMPOSITE:
                comparison_market = self._general.composite_market(
                    resolution.scope.atc4,
                    resolution.scope.filters,
                    comparison,
                    resolution.source,
                    measure,
                )
            else:
                comparison_market = self._general.market(
                    atc4, comparison, resolution.source, measure
                )
            assert_general_scope(
                comparison_market,
                resolution.scope.atc4,
                comparison,
                filters=resolution.scope.filters,
            )
            return general_comparison_result(
                market, comparison_market, metric, resolution
            )
        if name == "market.get_brand_metric":
            return general_metric_result(
                market,
                metric,
                general_metric_value(market, metric),
                resolution,
            )
        raise LookupError(f"unregistered market catalog tool: {name}")
