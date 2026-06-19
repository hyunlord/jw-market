from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.market_scope.catalog import MarketScopeCatalog
from pipeline.scripts.api.market_scope.cache import ScopeHashCache
from pipeline.scripts.api.market_scope.fact_collector import (
    FactIdentityIncompleteError,
    StrategyFact,
    collect_strategy_facts_from_mart,
    deduplicate_facts,
)
from pipeline.scripts.api.market_scope.normalize import normalize_market_scope
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload
from pipeline.scripts.api.market_scope.resolvers import StrategyScopeResolver
from pipeline.scripts.api.market_scope.types import (
    MarketScopeOption,
    MarketScopeRequest,
    MarketScopeValidationError,
    OptionType,
    ResolvedScope,
    ViewFamily,
)


def test_catalog_options_include_group_and_absent_members() -> None:
    catalog = MarketScopeCatalog.load_default()

    options = catalog.options_for_brand("트루패스", view_family=ViewFamily.STRATEGY)
    by_id = {option.option_id: option for option in options}

    assert "source:strategy_009" in by_id
    assert "group:trupass_family" in by_id
    group = by_id["group:trupass_family"]
    assert [member.brand_name for member in group.members if member.member_status == "absent_in_csd"] == [
        "피나스타",
        "제이다트",
    ]


def test_normalize_scope_is_order_independent_and_excludes_absent_members() -> None:
    catalog = MarketScopeCatalog.load_default()
    request_a = MarketScopeRequest(
        brand="트루패스",
        view_family=ViewFamily.STRATEGY,
        source="UBIST",
        measure="sales",
        option_ids=(" group:trupass_family ", "source:strategy_009", "group:trupass_family"),
    )
    request_b = MarketScopeRequest(
        brand="트루패스",
        view_family=ViewFamily.STRATEGY,
        source="UBIST",
        measure="sales",
        option_ids=("source:strategy_009", "group:trupass_family"),
    )

    resolved_a = normalize_market_scope(request_a, catalog.options_for_brand("트루패스", view_family=ViewFamily.STRATEGY))
    resolved_b = normalize_market_scope(request_b, catalog.options_for_brand("트루패스", view_family=ViewFamily.STRATEGY))

    assert resolved_a.scope_hash == resolved_b.scope_hash
    assert resolved_a.selected_option_ids == ("group:trupass_family", "source:strategy_009")
    assert resolved_a.resolved_source_markets == ("strategy_009",)
    assert [member.brand_name for member in resolved_a.excluded_members] == ["피나스타", "제이다트"]


def test_normalize_rejects_mixed_family_and_unavailable_source() -> None:
    catalog = MarketScopeCatalog.load_default()
    strategy = catalog.options_for_brand("리바로젯", view_family=ViewFamily.STRATEGY)
    general = MarketScopeOption(
        option_id="general_atc4:C10C0",
        label="C10C0",
        option_type=OptionType.GENERAL_ATC4,
        view_family=ViewFamily.GENERAL,
        source_markets=(),
        atc4_set=("C10C0",),
        members=(),
        member_status="present",
        available_sources=("UBIST",),
        catalog_version=catalog.catalog_version,
    )
    mixed_request = MarketScopeRequest(
        brand="리바로젯",
        view_family=ViewFamily.STRATEGY,
        source="UBIST",
        measure="sales",
        option_ids=("source:strategy_006", "general_atc4:C10C0"),
    )
    with pytest.raises(MarketScopeValidationError, match="mixed view_family"):
        normalize_market_scope(mixed_request, (*strategy, general))

    unavailable_source = MarketScopeRequest(
        brand="리바로젯",
        view_family=ViewFamily.STRATEGY,
        source="IQVIA",
        measure="sales",
        option_ids=("source:strategy_006",),
    )
    with pytest.raises(MarketScopeValidationError, match="not available"):
        normalize_market_scope(unavailable_source, strategy)


def test_deduplicate_facts_uses_raw_identity_not_market_id() -> None:
    facts = (
        _fact("strategy_006", "A", raw_fact_id="raw:a", value=100),
        _fact("strategy_007", "A", raw_fact_id="raw:a", value=100),
        _fact("strategy_007", "B", raw_fact_id="raw:b", value=50),
    )

    deduped, diagnostics = deduplicate_facts(facts)

    assert [fact.brand_key for fact in deduped] == ["A", "B"]
    assert diagnostics.candidate_fact_count == 3
    assert diagnostics.deduped_fact_count == 2
    assert diagnostics.dropped_duplicate_count == 1


def test_deduplicate_facts_requires_raw_identity_for_multi_scope() -> None:
    facts = (
        _fact("strategy_006", "A", raw_fact_id=None, value=100),
        _fact("strategy_007", "B", raw_fact_id="raw:b", value=50),
    )

    with pytest.raises(FactIdentityIncompleteError, match="raw fact identity"):
        deduplicate_facts(facts)


def test_disjoint_scope_dedup_keeps_simple_sum() -> None:
    facts = (
        _fact("strategy_006", "A", raw_fact_id="raw:a", value=100),
        _fact("strategy_007", "B", raw_fact_id="raw:b", value=50),
    )

    deduped, diagnostics = deduplicate_facts(facts)
    payload = recompute_strategy_payload(deduped, focus_brand_key="A", source="ubist", measure="sales")

    assert diagnostics.dropped_duplicate_count == 0
    assert _market_size_value(payload, "2026-01") == 150.0


def test_recompute_union_metrics_recalculates_ratios_and_rankings() -> None:
    facts = (
        _fact("strategy_006", "A", brand_name="Focus", company="JW", raw_fact_id="raw:a1", value=50),
        _fact("strategy_006", "B", brand_name="Other B", company="B Co", raw_fact_id="raw:b1", value=50),
        _fact("strategy_007", "A", brand_name="Focus", company="JW", raw_fact_id="raw:a2", value=150),
        _fact("strategy_007", "C", brand_name="Other C", company="C Co", raw_fact_id="raw:c1", value=50),
    )

    payload = recompute_strategy_payload(facts, focus_brand_key="A", source="ubist", measure="sales")
    data = payload["data"]

    assert _market_size_value(payload, "2026-01") == 300.0
    latest_ranking = data["brand_ranking_stacked"]["rankings_by_year"]["2026"]
    assert latest_ranking[0]["brand_key"] == "A"
    assert latest_ranking[0]["raw_value"] == 200.0
    assert latest_ranking[0]["ms"] == pytest.approx(66.6667)
    assert data["hhi_series_5y"] == []
    assert data["company_ranking_stacked"]["rankings_by_year"]["2026"][0]["company"] == "JW"
    assert data["ei_ms_matrix"]["data"][0]["ei_5y"] is None


def test_scope_hash_cache_copies_payload_by_scope_hash() -> None:
    cache = ScopeHashCache()
    original = {"result": {"nested": {"value": 1}}}
    cache.write("abc123", original)
    original["result"]["nested"]["value"] = 99

    cached = cache.read("abc123")
    assert cached == {"result": {"nested": {"value": 1}}}
    assert cached is not None
    cached["result"]["nested"]["value"] = 2
    assert cache.read("abc123") == {"result": {"nested": {"value": 1}}}


def test_strategy_resolver_uses_cache_fast_path_for_single_source_option() -> None:
    catalog = MarketScopeCatalog.load_default()
    cached_payload = {"brand": "리바로젯", "data": {"from_cache": True}, "market_id": "strategy_006"}

    resolver = StrategyScopeResolver(
        catalog=catalog,
        cache_reader=lambda request, resolved: cached_payload,
        fact_provider=lambda request, resolved: pytest.fail("single-market fast path must not collect facts"),
    )
    request = MarketScopeRequest(
        brand="리바로젯",
        view_family=ViewFamily.STRATEGY,
        source="UBIST",
        measure="sales",
        option_ids=("source:strategy_006",),
    )

    response = resolver.cause(request)

    assert response["result"] == cached_payload
    assert response["resolved_scope"]["resolved_source_markets"] == ["strategy_006"]
    assert response["resolved_scope"]["dedup"]["dedup_strategy"] == "legacy_cache_fast_path_v1"


def test_strategy_resolver_passes_canonical_source_to_dependencies() -> None:
    catalog = MarketScopeCatalog.load_default()
    seen: dict[str, str] = {}

    def cache_reader(request: MarketScopeRequest, resolved: ResolvedScope) -> dict[str, bool]:
        del resolved
        seen["source"] = request.source
        seen["measure"] = request.measure
        return {"ok": True}

    resolver = StrategyScopeResolver(
        catalog=catalog,
        cache_reader=cache_reader,
        fact_provider=lambda request, resolved: pytest.fail("single-market fast path must not collect facts"),
    )
    request = MarketScopeRequest(
        brand="페린젝트",
        view_family=ViewFamily.STRATEGY,
        source="nsa",
        measure="sales",
        option_ids=("source:strategy_012",),
    )

    resolver.cause(request)

    assert seen == {"source": "IQVIA", "measure": "sales"}


def test_mart_collector_maps_contract_source_to_mart_source() -> None:
    captured: dict[str, str | Sequence[str]] = {}

    def fetch_all(sql: str, params: Sequence[str]) -> list[dict[str, str]]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    collect_strategy_facts_from_mart(
        fetch_all,
        mart_db="jw_mart",
        source_markets=("strategy_012",),
        source="IQVIA",
        measure="sales",
    )

    assert captured["params"] == ("ml_012", "iqvia_nsa", "sales")


def test_mart_collector_rejects_unsafe_mart_db_identifier() -> None:
    with pytest.raises(Exception, match="unsafe SQL identifier"):
        collect_strategy_facts_from_mart(
            lambda sql, params: pytest.fail("unsafe db name must fail before SQL execution"),
            mart_db="jw_mart`; DROP TABLE cache_cause; --",
            source_markets=("strategy_012",),
            source="IQVIA",
            measure="sales",
        )


def _fact(
    market_id: str,
    brand_key: str,
    *,
    raw_fact_id: str | None,
    value: float,
    brand_name: str | None = None,
    company: str = "Unknown",
) -> StrategyFact:
    return StrategyFact(
        market_id=market_id,
        raw_fact_id=raw_fact_id,
        brand_key=brand_key,
        brand_name=brand_name or brand_key,
        company=company,
        source="ubist",
        measure="sales",
        unit_label="KRW",
        raw_value_history={"2025-01": value / 2, "2026-01": value},
    )


def _market_size_value(payload: dict[str, object], period: str) -> float:
    """Return one FE-facing market-size point value from a recompute payload."""

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
