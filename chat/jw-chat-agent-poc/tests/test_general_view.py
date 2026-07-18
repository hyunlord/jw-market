from __future__ import annotations

from dataclasses import replace

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.service.general_view_routing import (
    GeneralRoute,
    GeneralViewService,
    _atc4_code,
    _brand_hint,
    _source,
)
from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    GeneralMarket,
    GeneralViewBrandMismatchError,
    GeneralViewBackendError,
    parse_general_market_response,
)
from jw_chat_agent_poc.tools.general_view_membership import (
    GeneralBrandMembership,
    GeneralMembershipLoadError,
    StaticGeneralMembershipReader,
    TtlGeneralMembershipCache,
)
from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


def _payload(*, atc4: str = "C10A1", source: str = "ubist", measure: str = "sales") -> dict:
    return {
        "status": "SUCCESS",
        "result": {
            "view": "market_landscape",
            "source": "UBIST",
            "measure": measure,
            "unit_label": "KRW",
            "market_meta": {
                "market_name": f"동적 시장: ATC4 {atc4}",
                "market_definition_label": f"동적 시장: ATC4 {atc4}",
                "filters": {
                    "view": "general",
                    "atc4": [atc4],
                    "source": source,
                    "measure": measure,
                },
            },
            "data": {
                "kpi": {
                    "market_size_recent": 100_000_000_000,
                    "target_brand": "리바로",
                    "brand_value_recent": 8_000_000_000,
                    "target_share_pct": 8.0,
                    "target_rank": 2,
                },
                "sources_data": {
                    "market_size_series": [{"period": "2026-04", "value": 100_000_000_000}],
                },
                "ei_ms_matrix": {
                    "data": [
                        {
                            "brand": "리피토",
                            "rank": 1,
                            "value_recent": 15_000_000_000,
                            "share_pct": 15.0,
                        },
                        {
                            "brand": "리바로",
                            "rank": 2,
                            "value_recent": 8_000_000_000,
                            "share_pct": 8.0,
                        },
                    ]
                },
                "brand_ranking": {
                    "yearly": [
                        {
                            "year": 2026,
                            "rankings": [
                                {"brand": "리피토", "rank": 1, "value": 75_000_000_000, "ms_pct": 15.0},
                                {"brand": "리바로", "rank": 2, "value": 41_000_000_000, "ms_pct": 8.0},
                            ],
                        }
                    ]
                },
            },
        },
    }


def test_parse_general_market_response_accepts_mislabeled_top_level_view() -> None:
    payload = _payload()
    payload["result"]["source"] = "WRONG_TOP_LEVEL_SOURCE"
    payload["result"]["measure"] = "wrong_top_level_measure"
    market = parse_general_market_response(
        payload, requested_atc4="C10A1", requested_source="ubist", requested_measure="sales"
    )

    assert market.view_type == "general_view"
    assert market.market_basis == "ATC4"
    assert market.atc4_code == "C10A1"
    assert market.source == "UBIST"
    assert market.measure == "sales"
    assert market.brand_share_pct == 8.0


def test_parse_general_market_response_uses_current_period_matrix_row() -> None:
    payload = _payload()
    payload["result"]["data"]["kpi"].update(
        {
            "target_brand": "리피토",
            "brand_value_recent": 15_000_000_000,
            "target_share_pct": 15.0,
            "target_rank": 1,
        }
    )

    market = parse_general_market_response(
        payload,
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
        requested_brand="리바로",
    )

    assert market.brand == "리바로"
    assert market.brand_value == 8_000_000_000
    assert market.brand_share_pct == 8.0
    assert market.brand_rank == 2
    assert market.top_brands[0].value == 15_000_000_000


def test_parse_general_market_response_preserves_zero_current_metrics() -> None:
    payload = _payload()
    payload["result"]["data"]["ei_ms_matrix"]["data"][1].update(
        {"rank": 0, "value_recent": 0, "share_pct": 0}
    )

    market = parse_general_market_response(
        payload,
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
        requested_brand="리바로",
    )

    assert market.brand_value == 0.0
    assert market.brand_share_pct == 0.0
    assert market.brand_rank == 0


def test_parse_general_market_response_fails_closed_when_requested_brand_is_missing() -> None:
    payload = _payload()
    payload["result"]["data"]["brand_ranking"]["yearly"][-1]["rankings"].append(
        {"brand": "없는 브랜드", "rank": 99, "value": 1, "ms_pct": 0.01}
    )

    with pytest.raises(GeneralViewBackendError, match="brand mismatch"):
        parse_general_market_response(
            payload,
            requested_atc4="C10A1",
            requested_source="ubist",
            requested_measure="sales",
            requested_brand="없는 브랜드",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("atc4", ["C10C0"]), ("source", "iqvia_nsa"), ("measure", "volume"), ("view", "strategic")),
)
def test_parse_general_market_response_fails_closed_on_scope_mismatch(field: str, value: object) -> None:
    payload = _payload()
    payload["result"]["market_meta"]["filters"][field] = value

    with pytest.raises(GeneralViewBackendError, match="scope mismatch"):
        parse_general_market_response(
            payload, requested_atc4="C10A1", requested_source="ubist", requested_measure="sales"
        )


class FakeBackend:
    def __init__(self) -> None:
        self.candidate_map: dict[tuple[str, str], tuple[AtcCandidate, ...]] = {}
        self.market_map: dict[str, GeneralMarket] = {}
        self.market_errors: dict[str, GeneralViewBackendError] = {}
        self.market_calls: list[tuple[str, str | None, str, str]] = []

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return self.candidate_map.get((brand, source), ())

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        self.market_calls.append((atc4, brand, source, measure))
        if atc4 in self.market_errors:
            raise self.market_errors[atc4]
        return self.market_map[atc4]


class StrategicMembership:
    def __init__(self, brands: set[str]) -> None:
        self._brands = brands

    def resolve(self, question: str, allow_default: bool = False):
        for brand in self._brands:
            if brand in question:
                return type("Resolution", (), {"canonical_brand": brand})()
        raise LookupError(question)


def test_membership_cache_preserves_all_atc4_sources_and_avoids_backend_candidate_scan() -> None:
    memberships = (
        GeneralBrandMembership("마운자로", "마운자로", "A10S0", "GLP-1", "iqvia"),
        GeneralBrandMembership("마운자로", "마운자로", "A10S0", "GLP-1", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    service = GeneralViewService(
        backend,
        StrategicMembership(set()),
        enabled=True,
        general_membership=cache,
    )
    backend.market_map["A10S0"] = _market("A10S0", 10.0)

    result = service.answer("IQVIA 마운자로 시장 점유율", compact=False, dual=False)

    assert result["general_view_contract"]["atc4_code"] == "A10S0"
    assert backend.candidate_map == {}
    assert cache.candidates("마운자로", "iqvia") == (AtcCandidate("A10S0", "GLP-1"),)


def test_membership_cache_exact_lookup_does_not_silently_match_unknown_brand() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (GeneralBrandMembership("마운자로", "마운자로", "A10S0", "GLP-1", "iqvia"),)
        ),
        ttl_seconds=300,
    )

    assert cache.candidates("마운자로", "iqvia")
    assert cache.candidates("마운", "iqvia") == ()


def test_membership_cache_resolves_unique_shorthand_to_canonical_brand_key() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (
                GeneralBrandMembership(
                    "중외5포도당생리식염액",
                    "중외5%포도당생리식염액",
                    "K01B3",
                    "혈액대용제",
                    "iqvia",
                ),
            )
        ),
        ttl_seconds=300,
    )

    resolution = cache.resolve("생리식염", "iqvia")

    assert resolution is not None
    assert resolution.brand_key == "중외5포도당생리식염액"
    assert resolution.candidates == (AtcCandidate("K01B3", "혈액대용제"),)


def test_membership_cache_fails_closed_for_ambiguous_shorthand() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (
                GeneralBrandMembership("생리식염액A", "생리식염액 A", "K01B3", "혈액대용제", "iqvia"),
                GeneralBrandMembership("생리식염액B", "생리식염액 B", "K01C1", "관류액", "iqvia"),
            )
        ),
        ttl_seconds=300,
    )

    assert cache.resolve("생리식염", "iqvia") is None


def test_general_view_queries_mart_with_resolved_canonical_brand_key() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (
                GeneralBrandMembership(
                    "중외5포도당생리식염액",
                    "중외5%포도당생리식염액",
                    "K01B3",
                    "혈액대용제",
                    "iqvia",
                ),
            )
        ),
        ttl_seconds=300,
    )
    backend = FakeBackend()
    backend.market_map["K01B3"] = _market("K01B3", 10.0)
    service = GeneralViewService(
        backend,
        StrategicMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("IQVIA 생리식염 시장 점유율", compact=False, dual=False)

    assert result["tool_calls"][0]["tool"] == "general_view_dynamic_market"
    assert backend.market_calls == [("K01B3", "중외5포도당생리식염액", "iqvia", "sales")]


class FailingGeneralMembership:
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        raise GeneralMembershipLoadError("membership unavailable")


def test_membership_load_failure_falls_back_to_existing_candidate_api() -> None:
    backend = FakeBackend()
    backend.candidate_map[("마운자로", "iqvia")] = (AtcCandidate("A10S0", "GLP-1"),)
    backend.market_map["A10S0"] = _market("A10S0", 10.0)
    service = GeneralViewService(
        backend,
        StrategicMembership(set()),
        enabled=True,
        general_membership=FailingGeneralMembership(),
    )

    result = service.answer("IQVIA 마운자로 시장 점유율", compact=False, dual=False)

    assert result["general_view_contract"]["atc4_code"] == "A10S0"


def _market(atc4: str, brand_value: float) -> GeneralMarket:
    parsed = parse_general_market_response(
        _payload(atc4=atc4), requested_atc4=atc4, requested_source="ubist", requested_measure="sales"
    )
    return replace(parsed, brand_value=brand_value)


def test_recent_one_year_market_size_uses_twelve_month_sum_and_names_window() -> None:
    backend = FakeBackend()
    backend.candidate_map[("리바로", "ubist")] = (AtcCandidate("C10A1", "지질조절제"),)
    market = _market("C10A1", 8_000_000_000.0)
    monthly = tuple((f"2025-{month:02d}", float(month) * 100_000_000) for month in range(1, 13))
    backend.market_map["C10A1"] = replace(
        market,
        period="2025-12",
        market_size=1_200_000_000.0,
        market_size_series=monthly,
    )
    service = GeneralViewService(backend, StrategicMembership(set()), enabled=True)

    result = service.answer("리바로 C10A1 시장의 최근 1년 규모", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["period"] == "최근 12개월 합계 2025-01~2025-12"
    assert "78.0억원" in result["answer"]
    assert "1.2억원" not in result["answer"]


def test_unspecified_market_period_keeps_latest_single_period() -> None:
    backend = FakeBackend()
    backend.candidate_map[("리바로", "ubist")] = (AtcCandidate("C10A1", "지질조절제"),)
    backend.market_map["C10A1"] = _market("C10A1", 8_000_000_000.0)
    service = GeneralViewService(backend, StrategicMembership(set()), enabled=True)

    result = service.answer("리바로 C10A1 시장 규모", compact=False, dual=False)

    assert "시장 규모 (2026-04)" in result["answer"]


def test_route_matrix_has_no_human_loop() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=True)

    assert service.route("리바로 ATC4 기준 점유율") is GeneralRoute.GENERAL_ONLY
    assert service.route("리바로 전략뷰 시장 점유율") is GeneralRoute.EXISTING
    assert service.route("ml_006 2025-04 시장규모") is GeneralRoute.EXISTING
    assert service.route("리바로 시장 점유율은?") is GeneralRoute.DUAL
    assert service.route("포도당 대한 시장 점유율은?") is GeneralRoute.GENERAL_ONLY


@pytest.mark.parametrize(
    "question",
    (
        "리바로랑 같은 시장 매출",
        "리바로가 속한 시장 매출",
        "리바로랑 같은 시장 규모",
        "리바로 시장 규모",
        "리바로와 같은 시장 전체 매출",
        "리바로가 소속된 시장 전체 매출",
        "리바로랑 같은 시장 총매출",
        "리바로가 포함된 시장 총매출",
        "리바로랑 같은 시장 전체 규모",
        "리바로 시장의 전체 규모",
    ),
)
def test_market_scope_intent_keeps_strategic_route_precedence(question: str) -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=True)

    assert service.route(question) is GeneralRoute.EXISTING


@pytest.mark.parametrize(
    "question",
    ("일반뷰 리바로랑 같은 시장 매출", "ATC4 기준 리바로가 속한 시장 매출"),
)
def test_explicit_general_view_signal_precedes_market_scope_intent(question: str) -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=True)

    assert service.route(question) is GeneralRoute.GENERAL_ONLY


def test_market_scope_intent_does_not_hide_general_only_membership() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=True)

    assert service.route("포도당이 속한 시장 매출") is GeneralRoute.GENERAL_ONLY


def test_general_only_market_scope_resolves_canonical_brand_end_to_end() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (GeneralBrandMembership("포도당", "포도당", "B05X0", "정맥주사액", "ubist"),)
        ),
        ttl_seconds=300,
    )
    backend = FakeBackend()
    backend.market_map["B05X0"] = _market("B05X0", 10.0)
    service = GeneralViewService(
        backend,
        StrategicMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("포도당이 속한 시장 매출", compact=False, dual=False)

    assert result["general_view_contract"]["atc4_code"] == "B05X0"
    assert backend.market_calls == [("B05X0", "포도당", "ubist", "sales")]


@pytest.mark.parametrize(
    "question",
    (
        "포도당 시장 규모 추이",
        "포도당 시장 규모 비교",
        "포도당이 속한 시장 최신 뉴스",
    ),
)
def test_general_only_market_scope_keeps_analytic_questions_on_existing_path(question: str) -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership(set()), enabled=True)

    assert service.route(question) is GeneralRoute.EXISTING


@pytest.mark.parametrize("brand", ("가나톤", "가나릴", "가나텍", "가네골드"))
def test_brand_hint_preserves_brand_names_starting_with_korean_particle_characters(brand: str) -> None:
    assert _brand_hint(f"일반뷰 ATC4 기준 {brand} 시장점유율") == brand


@pytest.mark.parametrize("particle", ("은", "는", "이", "가", "을", "를"))
def test_brand_hint_removes_only_a_trailing_korean_particle(particle: str) -> None:
    assert _brand_hint(f"일반뷰 기준 리바로{particle} 시장점유율") == "리바로"


def test_unqualified_brand_uses_its_only_available_membership_source() -> None:
    memberships = (
        GeneralBrandMembership("마운자로", "마운자로", "A10S0", "GLP-1", "iqvia"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["A10S0"] = _market("A10S0", 10.0)
    service = GeneralViewService(
        backend,
        StrategicMembership({"마운자로"}),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("마운자로 시장점유율은?", compact=True, dual=True)

    assert backend.market_calls == [("A10S0", "마운자로", "iqvia", "sales")]
    assert result["general_view_contract"]["atc4_code"] == "A10S0"


def test_explicit_source_does_not_fallback_to_another_membership_source() -> None:
    memberships = (
        GeneralBrandMembership("마운자로", "마운자로", "A10S0", "GLP-1", "iqvia"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    service = GeneralViewService(
        backend,
        StrategicMembership({"마운자로"}),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("UBIST 마운자로 시장점유율은?", compact=True, dual=True)

    assert backend.market_calls == []
    assert result["general_view_contract"]["unavailable"] is True


def test_market_scope_dual_route_uses_catalog_membership_beyond_jw25(monkeypatch) -> None:
    monkeypatch.setenv("GENERAL_VIEW_ENABLED", "true")
    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            ({"brand": "마운자로", "market_id": "ml_003", "market_name": "당뇨 시장"},)
        ),
        ttl_seconds=300,
    )
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
    )

    assert resolver.general_route("마운자로 시장점유율은?") is GeneralRoute.DUAL


@pytest.mark.parametrize("suffix", ("에서", "의", "는", "를", "시장", "기준"))
def test_atc4_code_accepts_korean_suffixes(suffix: str) -> None:
    assert _atc4_code(f"C10A1{suffix} 리바로 매출") == "C10A1"


@pytest.mark.parametrize("question", ("NSA 기준 C10A1 시장", "nsa 기준 C10A1 시장", "IQVIA 기준 C10A1"))
def test_source_recognizes_iqvia_aliases(question: str) -> None:
    assert _source(question) == "iqvia"


@pytest.mark.parametrize("question", ("XC10A1", "C10A11", "C10A1X", "ABC123", "문서A10B20값"))
def test_atc4_code_rejects_alphanumeric_false_positives(question: str) -> None:
    assert _atc4_code(question) is None


def test_nsa_and_korean_suffix_reach_backend_with_clean_scope() -> None:
    backend = FakeBackend()
    backend.market_map["C10A1"] = _market("C10A1", 8_000_000_000)
    service = GeneralViewService(backend, StrategicMembership({"리바로"}), enabled=True)

    service.answer("NSA 기준 C10A1에서 리바로 매출 알려줘", compact=True, dual=False)

    assert backend.market_calls == [("C10A1", "리바로", "iqvia", "sales")]


def test_general_view_fix_does_not_change_strategic_route_precedence() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=True)

    assert service.route("리바로 경쟁역학 CD 시장점유율") is GeneralRoute.EXISTING


def test_disabled_route_is_existing_for_byte_compatible_behavior() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=False)

    assert service.route("리바로 ATC4 기준 점유율") is GeneralRoute.EXISTING
    assert service.route("리바로 시장 점유율은?") is GeneralRoute.EXISTING


@pytest.mark.parametrize(
    "question",
    (
        "리바로와 리바로젯의 최근 6개월 매출 추이를 비교해줘",
        "리바로 시장의 경쟁 구도가 최근 어떻게 변하고 있어?",
        "리바로 매출의 작년 동기 대비 성장률은?",
        "리바로 시장은 집중된 시장이야, 분산된 시장이야?",
        "리바로가 점유율 4%를 회복하려면 매출이 얼마나 늘어야 해?",
    ),
)
def test_existing_strategic_bq_signals_do_not_gain_general_view(question: str) -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로", "리바로젯"}), enabled=True)

    assert service.route(question) is GeneralRoute.EXISTING


def test_multi_atc_selects_largest_brand_sales_without_union() -> None:
    backend = FakeBackend()
    backend.candidate_map[("포도당 대한", "iqvia")] = (
        AtcCandidate("K01B3", "동적 시장: ATC4 K01B3"),
        AtcCandidate("K01C1", "동적 시장: ATC4 K01C1"),
        AtcCandidate("K04B1", "동적 시장: ATC4 K04B1"),
    )
    backend.market_map = {
        "K01B3": _market("K01B3", 20.0),
        "K01C1": _market("K01C1", 50.0),
        "K04B1": _market("K04B1", 30.0),
    }
    service = GeneralViewService(backend, StrategicMembership(set()), enabled=True)

    result = service.answer("IQVIA 포도당 대한 시장 점유율", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["atc4_code"] == "K01C1"
    assert contract["other_atc4_candidates"] == [
        "K01B3 (동적 시장: ATC4 K01B3)",
        "K04B1 (동적 시장: ATC4 K04B1)",
    ]
    assert contract["market_basis"] == "ATC4"
    assert "union" not in contract


def test_multi_atc_discards_only_candidates_without_requested_brand() -> None:
    backend = FakeBackend()
    backend.candidate_map[("포도당 대한", "iqvia")] = (
        AtcCandidate("K01B3", "ATC4 K01B3"),
        AtcCandidate("K04B1", "ATC4 K04B1"),
    )
    backend.market_map["K01B3"] = _market("K01B3", 50.0)
    backend.market_errors["K04B1"] = GeneralViewBrandMismatchError(
        "general-view brand mismatch: requested brand is absent from ranking"
    )
    service = GeneralViewService(backend, StrategicMembership(set()), enabled=True)

    result = service.answer("IQVIA 포도당 대한 시장 점유율", compact=False, dual=False)

    assert result["general_view_contract"]["atc4_code"] == "K01B3"
    assert result["tool_calls"][0]["tool"] == "general_view_dynamic_market"


def test_contract_appends_scope_label_and_dual_warning_idempotently() -> None:
    contract = {
        "mode": "dual",
        "view_type": "general_view",
        "market_basis": "ATC4",
        "atc4_code": "C10A1",
        "atc4_description": "동적 시장: ATC4 C10A1",
        "source": "UBIST",
        "measure": "sales",
        "unit": "KRW",
        "period": "2026-04",
        "share_denominator": "ATC4 C10A1 시장 전체 매출",
        "section_markdown": "## 일반뷰 (ATC4)\n\ncompact",
        "other_atc4_candidates": [],
    }

    once = enforce_general_view_contract("전략 답변", contract)
    twice = enforce_general_view_contract(once, contract)

    assert once == twice
    assert once.startswith("## 전략뷰 (market_landscape)\n\n전략 답변")
    assert "기준: 일반뷰 (ATC4 C10A1) | 소스: UBIST | 지표: sales | 기준: 2026-04" in once
    assert "전략뷰와 일반뷰는 시장 구성과 분모가 달라 수치를 직접 비교할 수 없습니다" in once


def test_tool_schema_exposes_general_view_only_when_enabled(monkeypatch) -> None:
    monkeypatch.delenv("GENERAL_VIEW_ENABLED", raising=False)
    disabled = tool_schemas(("리바로",), ("2026-04",))[0]["function"]["parameters"]["properties"]["view"]["enum"]
    monkeypatch.setenv("GENERAL_VIEW_ENABLED", "true")
    enabled = tool_schemas(("리바로",), ("2026-04",))[0]["function"]["parameters"]["properties"]["view"]["enum"]

    assert disabled == ["market_landscape", "competitive_dynamics"]
    assert enabled == ["market_landscape", "competitive_dynamics", "general_view"]


class RoutingResolver:
    def __init__(self, route: GeneralRoute, general: dict) -> None:
        self._route = route
        self._general = general
        self.general_calls: list[tuple[str, bool, bool]] = []

    def general_route(self, question: str) -> GeneralRoute:
        return self._route

    def answer_general(self, question: str, *, compact: bool, dual: bool) -> dict:
        self.general_calls.append((question, compact, dual))
        return self._general


def test_general_only_bypasses_existing_answer_path(monkeypatch) -> None:
    resolver = RoutingResolver(
        GeneralRoute.GENERAL_ONLY,
        {"question": "외부 브랜드 시장 점유율", "general_view_ready": True, "answer": "general"},
    )

    def fail_existing(*args, **kwargs):
        raise AssertionError("general-only must not fall back to a strategic answer")

    monkeypatch.setattr(service_app, "_answer_existing_without_pending", fail_existing)
    result = service_app._answer_without_pending(
        resolver,
        lambda **kwargs: None,
        "conversation",
        "외부 브랜드 시장 점유율",
        "live",
        None,
        service_app.SessionStore(),
    )

    assert result["answer"] == "general"
    assert resolver.general_calls == [("외부 브랜드 시장 점유율", False, False)]


def test_dual_answer_forces_strategic_primary_and_preserves_original_question(monkeypatch) -> None:
    resolver = RoutingResolver(
        GeneralRoute.DUAL,
        {
            "tool_calls": [{"tool": "general_view_dynamic_market"}],
            "sources": ["UBIST"],
            "general_view_contract": {"mode": "dual", "view_type": "general_view"},
        },
    )
    observed: dict[str, str] = {}

    def fake_existing(resolver, agent_factory, conversation_id, question, *args, **kwargs):
        observed["question"] = question
        return {
            "question": question,
            "answer": "strategic",
            "tool_calls": [{"tool": "get_brand_metric"}],
            "sources": ["cache"],
            "router_diagnostics": {"deterministic": True},
        }

    monkeypatch.setattr(service_app, "_answer_existing_without_pending", fake_existing)
    result = service_app._answer_without_pending(
        resolver,
        lambda **kwargs: None,
        "conversation",
        "리바로 시장 점유율은?",
        "live",
        None,
        service_app.SessionStore(),
    )

    assert "전략뷰(market_landscape) 기준" in observed["question"]
    assert result["question"] == "리바로 시장 점유율은?"
    assert [call["tool"] for call in result["tool_calls"]] == [
        "get_brand_metric",
        "general_view_dynamic_market",
    ]
    assert result["sources"] == ["cache", "UBIST"]
    assert result["router_diagnostics"]["general_view_mode"] == "dual"
    assert resolver.general_calls == [("리바로 시장 점유율은?", True, True)]
