from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies
from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.resolver.brand_resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader


class StaticMembershipReader:
    def __init__(self, rows: tuple[dict[str, str], ...]) -> None:
        self.rows = rows
        self.calls = 0

    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        self.calls += 1
        return self.rows


def _cache_reader() -> StaticMetricsCacheReader:
    return StaticMetricsCacheReader(
        cache_brands=[{"brand": "리바로", "market_id": "strategy_006", "market_name": "스타틴 시장"}],
        market_status=[],
    )


def test_cache_resolver_adds_mart_membership_brands() -> None:
    memberships = StaticMembershipReader(
        (
            {"brand": "피타틴", "market_id": "ml_006", "market_name": "스타틴 시장"},
            {"brand": "건피타", "market_id": "ml_006", "market_name": "스타틴 시장"},
            {"brand": "광동 아토르바스타틴", "market_id": "ml_006", "market_name": "스타틴 시장"},
            {"brand": "로수젯", "market_id": "ml_006", "market_name": "스타틴 시장"},
        )
    )
    resolver = BrandResolver(mode="cache", brand_reader=_cache_reader(), membership_reader=memberships)

    assert resolver.supported_brand_count() == 5
    for brand in ("피타틴", "건피타", "광동 아토르바스타틴", "로수젯"):
        resolution = resolver.resolve(brand, allow_default=False)
        assert resolution.canonical_brand == brand
        assert resolution.market_id == "ml_006"
        assert resolution.support_source == "mart_membership"


def test_cache_brand_metadata_wins_over_mart_membership() -> None:
    memberships = StaticMembershipReader(
        ({"brand": "리바로", "market_id": "ml_006", "market_name": "스타틴 시장"},)
    )
    resolver = BrandResolver(mode="cache", brand_reader=_cache_reader(), membership_reader=memberships)

    resolution = resolver.resolve("리바로", allow_default=False)

    assert resolver.supported_brand_count() == 1
    assert resolution.molecule_en == ("pitavastatin",)
    assert resolution.support_source.startswith("cache_brands")


def test_unknown_brand_remains_unsupported_with_mart_membership() -> None:
    memberships = StaticMembershipReader(
        ({"brand": "피타틴", "market_id": "ml_006", "market_name": "스타틴 시장"},)
    )
    resolver = BrandResolver(mode="cache", brand_reader=_cache_reader(), membership_reader=memberships)

    with pytest.raises(UnsupportedBrandError):
        resolver.resolve("없는브랜드123", allow_default=False)


def test_factory_wires_query_layer_as_membership_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")
    dependencies = build_chat_agent_dependencies(external_mode="fixture")

    assert dependencies.query_layer is not None
    assert dependencies.resolver._membership_reader is dependencies.query_layer


def test_mart_membership_brand_uses_query_layer_for_simple_metric() -> None:
    class QueryLayer:
        def brand_metric(self, brand: str, metric: str, period: str) -> dict[str, object]:
            return {"source": "UBIST", "tool": "get_brand_metric", "render_data": {"brand": brand, "metric": metric}}

    class CacheMetrics:
        def get_brand_metric(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("mart-only brand must not fall back to cache_brands metrics")

    agent = ChatAgent(metrics=CacheMetrics(), query_layer=QueryLayer())

    call = agent._metric_call("피타틴", metric="sales", filter_entries=(), prefer_mart=True)

    assert call["source"] == "UBIST"
    assert call["render_data"]["brand"] == "피타틴"
