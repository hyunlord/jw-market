from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies
from jw_chat_agent_poc.orchestrator.agent import ChatAgent, _prefer_mart_metric
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


def test_cache_resolver_adds_catalog_membership_brands() -> None:
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
        assert resolution.support_source == "catalog_membership"


def test_cache_brand_metadata_wins_over_catalog_membership() -> None:
    memberships = StaticMembershipReader(
        ({"brand": "리바로", "market_id": "ml_006", "market_name": "스타틴 시장"},)
    )
    resolver = BrandResolver(mode="cache", brand_reader=_cache_reader(), membership_reader=memberships)

    resolution = resolver.resolve("리바로", allow_default=False)

    assert resolver.supported_brand_count() == 1
    assert resolution.molecule_en == ("pitavastatin",)
    assert resolution.support_source.startswith("cache_brands")


def test_unknown_brand_remains_unsupported_with_catalog_membership() -> None:
    memberships = StaticMembershipReader(
        ({"brand": "피타틴", "market_id": "ml_006", "market_name": "스타틴 시장"},)
    )
    resolver = BrandResolver(mode="cache", brand_reader=_cache_reader(), membership_reader=memberships)

    with pytest.raises(UnsupportedBrandError):
        resolver.resolve("없는브랜드123", allow_default=False)


def test_factory_wires_catalog_as_membership_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")
    dependencies = build_chat_agent_dependencies(external_mode="fixture")

    assert dependencies.query_layer is not None
    assert dependencies.resolver._membership_reader is not dependencies.query_layer
    assert dependencies.resolver._membership_reader.__class__.__name__ == "TtlCatalogMembershipReader"


@pytest.mark.parametrize(
    "support_source",
    ("catalog_membership", "mart_membership", "cache_brands", "cache_brands+fixture_sidecar"),
)
def test_serving_resolver_sources_prefer_current_mart_metrics(support_source: str) -> None:
    assert _prefer_mart_metric(support_source) is True


def test_fixture_source_keeps_fixture_metric_path() -> None:
    assert _prefer_mart_metric("fixture") is False


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


def test_catalog_membership_brand_uses_query_layer_for_latest_metric() -> None:
    class QueryLayer:
        def brand_metric(self, brand: str, metric: str, period: str) -> dict[str, object]:
            return {
                "source": "IQVIA",
                "tool": "get_brand_metric",
                "render_data": {"brand": brand, "metric": metric, "period": period},
            }

    class CacheMetrics:
        def get_brand_metric(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("catalog membership must not fall through to the JW25 cache")

    agent = ChatAgent(metrics=CacheMetrics(), query_layer=QueryLayer())

    call = agent._metric_call("마운자로", metric="sales", filter_entries=(), prefer_mart=True)

    assert call["source"] == "IQVIA"
    assert call["render_data"]["period"] == "latest"


def test_cache_brand_latest_metric_prefers_current_query_layer_snapshot() -> None:
    class QueryLayer:
        def brand_metric(self, brand: str, metric: str, period: str) -> dict[str, object]:
            return {
                "source": "UBIST",
                "tool": "get_brand_metric",
                "render_data": {"brand": brand, "metric": metric, "period": "2026-05", "sales_억원": 80.385988},
            }

    class CacheMetrics:
        def get_brand_metric(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("latest metrics must not read the stale 2026-04 cache card")

    agent = ChatAgent(metrics=CacheMetrics(), query_layer=QueryLayer())

    call = agent._metric_call("리바로", metric="sales", filter_entries=(), prefer_mart=True)

    assert call["render_data"]["period"] == "2026-05"
    assert call["render_data"]["sales_억원"] == 80.385988


def test_past_period_metric_uses_query_layer_without_split_market_structure() -> None:
    class Catalog:
        market_structure: dict[str, str] = {}

    class QueryLayer:
        def catalog_for_brand(self, _brand: str) -> Catalog:
            return Catalog()

        def brand_metric(self, brand: str, metric: str, period: str) -> dict[str, object]:
            return {
                "source": "UBIST",
                "tool": "get_brand_metric",
                "render_data": {"brand": brand, "metric": metric, "period": period, "sales_억원": 83.18},
            }

    class CacheMetrics:
        def get_brand_metric(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("an explicit historical period must not read the latest cache card")

    agent = ChatAgent(metrics=CacheMetrics(), query_layer=QueryLayer())

    call = agent._metric_call("리바로", metric="sales", filter_entries=(("period_month", "2025-04"),))

    assert call["render_data"]["period"] == "2025-04"
    assert call["render_data"]["sales_억원"] == 83.18


@pytest.mark.parametrize(
    ("relative_range", "expected_months"),
    (("최근 3개월", 3), ("최근 12개월", 12), ("최근 1년", 12)),
)
def test_relative_range_uses_query_layer_trend_without_cache_fallback(
    relative_range: str,
    expected_months: int,
) -> None:
    class QueryLayer:
        def query(self, spec: dict[str, object], fallback_brand: str) -> dict[str, object]:
            return {
                "source": "UBIST",
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": fallback_brand,
                    "period": "2025-12→2026-05",
                    "query_spec": spec,
                    "level_top5_trend_series": [{"brand": fallback_brand, "series": [{"period": "2026-05"}]}],
                },
            }

    class CacheMetrics:
        def get_brand_metric(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("relative ranges must never fall back to the stale cache")

    agent = ChatAgent(metrics=CacheMetrics(), query_layer=QueryLayer())

    call = agent._metric_call(
        "리바로",
        metric="market_share",
        filter_entries=(("relative_range", relative_range),),
    )

    spec = call["render_data"]["query_spec"]
    assert spec["derive"] == ["average"]
    assert spec["filters"] == {"brand": "리바로", "periods": expected_months}
    assert "2026-04" not in str(call)
    assert "84.93" not in str(call)


def test_query_layer_failure_returns_query_failed_instead_of_cache_or_exception() -> None:
    class QueryLayer:
        def query(self, spec: dict[str, object], fallback_brand: str) -> dict[str, object]:
            raise LookupError("simulated query-layer route failure")

    class CacheMetrics:
        def get_brand_metric(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("query failures must not be hidden by cache fallback")

    agent = ChatAgent(metrics=CacheMetrics(), query_layer=QueryLayer())

    call = agent._metric_call(
        "리바로",
        metric="market_share",
        filter_entries=(("relative_range", "최근 6개월"),),
    )

    assert call["tool"] == "query_failed"
    assert call["render_data"]["status"] == "query_failed"
    assert call["render_data"]["requested_filters"] == {"relative_range": "최근 6개월"}
    assert call["source"] != "cache"
