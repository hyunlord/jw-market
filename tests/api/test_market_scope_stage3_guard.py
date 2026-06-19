from __future__ import annotations

from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.market_scope.catalog import MarketScopeCatalog
from pipeline.scripts.api.market_scope.fact_collector import OverlapWithoutFactIdentityError, StrategyFact
from pipeline.scripts.api.market_scope.resolvers import StrategyScopeResolver
from pipeline.scripts.api.market_scope.types import MarketScopeRequest, ViewFamily


def test_strategy_resolver_allows_missing_raw_identity_when_brand_keys_are_disjoint() -> None:
    catalog = MarketScopeCatalog.load_default()
    resolver = StrategyScopeResolver(
        catalog=catalog,
        cache_reader=lambda request, resolved: pytest.fail("group scope must not hit cache fast path"),
        fact_provider=lambda request, resolved: (
            _fact("strategy_006", "A", value=100),
            _fact("strategy_007", "B", value=50),
        ),
    )
    request = MarketScopeRequest(
        brand="리바로젯",
        view_family=ViewFamily.STRATEGY,
        source="UBIST",
        measure="sales",
        option_ids=("group:livalo_family",),
    )

    response = resolver.cause(request)

    assert _market_size_value(response["result"], "2026-01") == 150.0
    assert response["resolved_scope"]["dedup"]["disjoint"] is True
    assert response["resolved_scope"]["dedup"]["overlap_brand_key_count"] == 0


def test_strategy_resolver_blocks_missing_raw_identity_only_when_brand_keys_overlap() -> None:
    catalog = MarketScopeCatalog.load_default()
    resolver = StrategyScopeResolver(
        catalog=catalog,
        cache_reader=lambda request, resolved: pytest.fail("group scope must not hit cache fast path"),
        fact_provider=lambda request, resolved: (
            _fact("strategy_006", "A", value=100),
            _fact("strategy_007", "A", value=50),
        ),
    )
    request = MarketScopeRequest(
        brand="리바로젯",
        view_family=ViewFamily.STRATEGY,
        source="UBIST",
        measure="sales",
        option_ids=("group:livalo_family",),
    )

    with pytest.raises(OverlapWithoutFactIdentityError, match="overlap"):
        resolver.cause(request)


def _fact(market_id: str, brand_key: str, *, value: float) -> StrategyFact:
    return StrategyFact(
        market_id=market_id,
        raw_fact_id=None,
        brand_key=brand_key,
        brand_name=brand_key,
        company="Unknown",
        source="ubist",
        measure="sales",
        unit_label="KRW",
        raw_value_history={"2025-01": value / 2, "2026-01": value},
    )


def _market_size_value(payload: object, period: str) -> float:
    """Return one FE-facing market-size point value from a resolver response."""

    assert isinstance(payload, dict)
    data = payload["data"]
    assert isinstance(data, dict)
    series = data["market_size_series"]
    assert isinstance(series, list)
    values = {
        str(point["period"]): float(point["value"])
        for point in series
        if isinstance(point, dict)
    }
    return values[period]
