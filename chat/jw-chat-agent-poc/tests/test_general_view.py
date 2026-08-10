from __future__ import annotations

from dataclasses import replace

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.orchestrator.provenance import evidence_from_calls
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.general_view_routing import (
    GeneralRoute,
    GeneralViewService,
    StrategicMarketDefinition,
    _atc4_code,
    _brand_hint,
    _source,
)
from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.conversation_context import (
    extract_conversation_slots,
    resolve_anaphora,
)
from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    BrandMetricPoint,
    GeneralMarket,
    GeneralViewBrandMismatchError,
    GeneralViewBackendError,
    TopBrand,
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
from jw_chat_agent_poc.tools.metrics import market_scope_intent
from jw_chat_agent_poc.tools.metrics.market_scope_intent import asks_market_members


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


def test_fixture_resolver_exposes_strategic_market_members_for_reverse_mapping() -> None:
    members = BrandResolver(mode="fixture").market_members("고지혈증 시장 일반뷰로는?")

    assert "리바로" in members
    assert "리바로젯" in members


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


def test_parse_general_market_response_uses_latest_year_by_value_not_payload_order() -> None:
    payload = _payload()
    payload["result"]["data"]["brand_ranking"]["yearly"].append(
        {
            "year": 2024,
            "rankings": [
                {"brand": f"과거브랜드{rank}", "rank": rank, "value": 1, "ms_pct": 1.0}
                for rank in range(1, 4)
            ],
        }
    )

    market = parse_general_market_response(
        payload,
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
    )

    assert [row.brand for row in market.member_brands] == ["리피토", "리바로"]


def test_parse_general_market_response_preserves_zero_current_metrics() -> None:
    payload = _payload()
    payload["result"]["data"]["ei_ms_matrix"]["data"][1].update(
        {"rank": 0, "value_recent": 0, "share_pct": 0}
    )
    payload["result"]["data"]["hhi_series_5y"] = [{"period": "2026-04", "hhi": 0.0}]
    payload["result"]["data"]["kpi"]["hhi_recent"] = 999.0

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
    assert market.hhi_recent == 0.0


def test_parse_general_market_response_does_not_relabel_misaligned_hhi_period() -> None:
    payload = _payload()
    payload["result"]["data"]["hhi_series_5y"] = [
        {"period": "2026-05", "hhi": 999.0}
    ]

    market = parse_general_market_response(
        payload,
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
    )

    assert market.period == "2026-04"
    assert market.market_size_period == "2026-04"
    assert market.hhi_recent is None
    assert market.hhi_period is None


def test_parse_general_market_response_keeps_full_population_separate_from_active() -> None:
    payload = _payload()
    payload["result"]["data"]["brand_ranking"]["yearly"][0]["rankings"].extend(
        {
            "brand": f"전체브랜드{rank}",
            "rank": rank,
            "value": 0,
            "ms_pct": 0.0,
        }
        for rank in range(3, 9)
    )

    market = parse_general_market_response(
        payload,
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
    )

    assert len(market.member_population or ()) == 8
    assert len(market.member_brands) == 8
    assert [row.brand for row in market.active_members] == ["리피토", "리바로"]
    assert [row.brand for row in market.display_members] == ["리피토", "리바로"]


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
                return type(
                    "Resolution",
                    (),
                    {"canonical_brand": brand, "market_id": "ml_test", "market_ids": ("ml_test",)},
                )()
        raise LookupError(question)


class StrategicMembershipWithExplicitMarket(StrategicMembership):
    def explicit_market(self, question: str) -> tuple[str, str] | None:
        if "고지혈증 시장" in question:
            return "ml_006", "고지혈증 치료제 시장"
        return None


class StrategicMembershipWithMarketMembers(StrategicMembershipWithExplicitMarket):
    def __init__(self, brands: set[str], market_members: tuple[str, ...] = ("리바로", "리바로젯")) -> None:
        super().__init__(brands)
        self._market_members = market_members

    def market_members(self, question: str) -> tuple[str, ...]:
        return self._market_members if self.explicit_market(question) else ()


class LiveLivaloStrategicMembership(StrategicMembershipWithMarketMembers):
    def resolve(self, question: str, allow_default: bool = False):
        if "리바로 리바로젯" in question:
            return type(
                "Resolution",
                (),
                {"canonical_brand": "리바로젯", "market_id": "ml_006", "market_ids": ("ml_006",)},
            )()
        return super().resolve(question, allow_default=allow_default)

    def explicit_market(self, question: str) -> tuple[str, str] | None:
        if "리바로 리바로젯" in question:
            return "ml_006", "리바로 리바로젯"
        return super().explicit_market(question)


class StaticStrategicMarketDefinitionReader:
    def __init__(
        self,
        definitions: dict[str, StrategicMarketDefinition | None],
        exact_markets: dict[str, tuple[str, str] | None] | None = None,
    ) -> None:
        self._definitions = definitions
        self._exact_markets = exact_markets or {}

    def resolve(self, market_id: str) -> StrategicMarketDefinition | None:
        return self._definitions.get(market_id)

    def resolve_exact_base(self, market_name: str) -> tuple[str, str] | None:
        return self._exact_markets.get(market_name)


def _live_livalo_general_view_service(
    definition: StrategicMarketDefinition | None,
) -> tuple[GeneralViewService, FakeBackend]:
    memberships = (
        GeneralBrandMembership("리바로", "리바로", "C10A1", "스타틴류", "ubist"),
        GeneralBrandMembership("리바로젯", "리바로젯", "C10C", "지질조절제 복합제제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["C10A1"] = replace(
        _market("C10A1", 10.0),
        atc4_description="스타틴류 (HMG-CoA 환원효소 억제제)",
        market_size=100_000_000_000,
        brand=None,
        brand_value=None,
    )
    backend.market_map["C10C"] = replace(
        _market("C10C", 20.0),
        atc4_description="지질조절제 복합제제",
        market_size=40_000_000_000,
        brand=None,
        brand_value=None,
    )
    service = GeneralViewService(
        backend,
        LiveLivaloStrategicMembership({"리바로", "리바로젯"}),
        enabled=True,
        general_membership=cache,
        market_definition_reader=StaticStrategicMarketDefinitionReader({"ml_006": definition}),
    )
    return service, backend


def test_explicit_strategic_market_uses_catalog_atc4_definition_before_brand_membership() -> None:
    service, backend = _live_livalo_general_view_service(
        StrategicMarketDefinition(
            market_id="ml_006",
            data_source="ubist",
            atc4_codes=("C10A1", "C10C"),
        )
    )

    result = service.answer("리바로 리바로젯 일반뷰로는?", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["atc4_codes"] == ["C10A1", "C10C"]
    assert [section["atc4_code"] for section in contract["atc4_sections"]] == ["C10A1", "C10C"]
    assert sorted(backend.market_calls) == [
        ("C10A1", None, "ubist", "sales"),
        ("C10C", None, "ubist", "sales"),
    ]


@pytest.mark.parametrize(
    ("definition", "expected_codes", "expected_source", "expected_reason", "expected_excluded"),
    (
        (
            StrategicMarketDefinition("ml_006", "ubist", ("C10A1", "C10C")),
            ["C10A1", "C10C"],
            "catalog_definition",
            None,
            0,
        ),
        (
            StrategicMarketDefinition("ml_006", "ubist", ("C10C",)),
            ["C10C"],
            "catalog_definition",
            None,
            0,
        ),
        (
            StrategicMarketDefinition("ml_006", "ubist", ("C10A1",), excluded_atc4_count=1),
            ["C10A1"],
            "catalog_definition",
            "catalog_code_invalid",
            1,
        ),
        (
            StrategicMarketDefinition("ml_006", "ubist", ()),
            ["C10C"],
            "brand_membership",
            "catalog_definition_empty",
            0,
        ),
        (
            None,
            ["C10C"],
            "brand_membership",
            "catalog_row_absent",
            0,
        ),
    ),
)
def test_strategic_market_definition_failure_injection_is_explicit(
    definition: StrategicMarketDefinition | None,
    expected_codes: list[str],
    expected_source: str,
    expected_reason: str | None,
    expected_excluded: int,
) -> None:
    service, _ = _live_livalo_general_view_service(definition)

    result = service.answer("리바로 리바로젯 일반뷰로는?", compact=False, dual=False)

    contract = result["general_view_contract"]
    actual_codes = contract.get("atc4_codes") or [contract["atc4_code"]]
    trace = result["tool_calls"][0]["qa_trace"]
    diagnostic = {
        "input_market": trace["input_market"],
        "atc4_source": trace["atc4_source"],
        "candidate_atc4_codes": trace["candidate_atc4_codes"],
        "member_brand_count": trace["member_brand_count"],
        "excluded_atc4_count": trace["excluded_atc4_count"],
        "reduction_reason": trace["reduction_reason"],
    }
    print(diagnostic)
    assert actual_codes == expected_codes
    assert diagnostic == {
        "input_market": "ml_006",
        "atc4_source": expected_source,
        "candidate_atc4_codes": expected_codes,
        "member_brand_count": 2,
        "excluded_atc4_count": expected_excluded,
        "reduction_reason": expected_reason,
    }
    assert "question" not in diagnostic
    assert "answer" not in diagnostic
    assert "fragment" not in diagnostic


def test_strategic_market_reverse_mapping_fails_closed_for_multiple_atc4_codes() -> None:
    brands = tuple(f"브랜드{index}" for index in range(1, 6))
    memberships = tuple(
        GeneralBrandMembership(brand, brand, f"A10A{index}", f"ATC4 {index}", "ubist")
        for index, brand in enumerate(brands, 1)
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    service = GeneralViewService(
        backend,
        StrategicMembershipWithMarketMembers(set(brands), brands),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("고지혈증 시장 일반뷰로는?", compact=False, dual=False)

    assert result["general_view_contract"]["unavailable"] is True
    assert "4개를 초과" in result["answer"]
    for index in range(1, 6):
        assert f"A10A{index}" in result["answer"]
    assert "ATC4를 지정" in result["answer"]
    assert backend.market_calls == []


def test_strategic_market_reverse_mapping_splits_two_atc4_general_views_without_aggregation() -> None:
    memberships = (
        GeneralBrandMembership("리바로", "리바로", "C10A1", "스타틴류", "ubist"),
        GeneralBrandMembership("리바로젯", "리바로젯", "C10A1", "스타틴류", "ubist"),
        GeneralBrandMembership("리바로젯", "리바로젯", "C10C", "지질조절제 복합제제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["C10A1"] = replace(
        _market("C10A1", 10.0),
        atc4_description="스타틴류 (HMG-CoA 환원효소 억제제)",
        market_size=100_000_000_000,
        brand=None,
        brand_value=None,
    )
    backend.market_map["C10C"] = replace(
        _market("C10C", 20.0),
        atc4_description="지질조절제 복합제제",
        market_size=40_000_000_000,
        brand=None,
        brand_value=None,
    )
    service = GeneralViewService(
        backend,
        StrategicMembershipWithMarketMembers({"리바로", "리바로젯"}),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("고지혈증 시장 일반뷰로는?", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["atc4_codes"] == ["C10A1", "C10C"]
    assert [section["atc4_code"] for section in contract["atc4_sections"]] == ["C10A1", "C10C"]
    assert [section["market_size"] for section in contract["atc4_sections"]] == [
        100_000_000_000,
        40_000_000_000,
    ]
    assert "각각의 일반뷰로 나눠 보여드립니다" in result["answer"]
    assert "ATC4 C10A1" in result["answer"]
    assert "ATC4 C10C" in result["answer"]
    assert "합계" not in result["answer"]
    assert "평균" not in result["answer"]
    assert sorted(backend.market_calls) == [
        ("C10A1", None, "ubist", "sales"),
        ("C10C", None, "ubist", "sales"),
    ]


def test_split_general_views_emit_independent_market_size_facts() -> None:
    memberships = (
        GeneralBrandMembership("리바로", "리바로", "C10A1", "스타틴류", "ubist"),
        GeneralBrandMembership("리바로젯", "리바로젯", "C10A1", "스타틴류", "ubist"),
        GeneralBrandMembership("리바로젯", "리바로젯", "C10C", "지질조절제 복합제제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["C10A1"] = replace(
        _market("C10A1", 10.0),
        market_size=100_000_000_000,
        brand=None,
        brand_value=None,
    )
    backend.market_map["C10C"] = replace(
        _market("C10C", 20.0),
        market_size=40_000_000_000,
        brand=None,
        brand_value=None,
    )
    service = GeneralViewService(
        backend,
        StrategicMembershipWithMarketMembers({"리바로", "리바로젯"}),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("고지혈증 시장 일반뷰로는?", compact=False, dual=False)
    facts = evidence_from_calls(result["tool_calls"], result["answer"])
    market_size_facts = [fact for fact in facts if fact.metric == "시장규모"]

    assert [
        (fact.entity, fact.metric, fact.unit, fact.value)
        for fact in market_size_facts
    ] == [
        ("C10A1", "시장규모", "억원", "1,000.00억원"),
        ("C10C", "시장규모", "억원", "400.00억원"),
    ]
    assert all(fact.value != "1,400.00억원" for fact in facts)


def test_strategic_market_reverse_mapping_keeps_typed_unavailable_when_atc4_resolution_fails() -> None:
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(()), ttl_seconds=300)
    backend = FakeBackend()
    service = GeneralViewService(
        backend,
        StrategicMembershipWithMarketMembers({"리바로", "리바로젯"}),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("고지혈증 시장 일반뷰로는?", compact=False, dual=False)

    assert result["general_view_contract"]["unavailable"] is True
    assert "대응하는 ATC4를 확인할 수 없습니다" in result["answer"]
    assert backend.market_calls == []


def test_strategic_market_reverse_mapping_uses_only_a_unique_atc4_code() -> None:
    memberships = (
        GeneralBrandMembership("리바로", "리바로", "C10A1", "스타틴", "ubist"),
        GeneralBrandMembership("리바로젯", "리바로젯", "C10A1", "스타틴", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["C10A1"] = _market("C10A1", 10.0)
    service = GeneralViewService(
        backend,
        StrategicMembershipWithMarketMembers({"리바로", "리바로젯"}),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("고지혈증 시장 일반뷰로는?", compact=False, dual=False)

    assert result["general_view_contract"]["atc4_code"] == "C10A1"
    assert backend.market_calls == [("C10A1", None, "ubist", "sales")]


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
    assert result["tool_calls"][0]["qa_trace"]["status"] == "ok"
    assert result["tool_calls"][0]["qa_trace"]["row_count"] > 0
    assert result["tool_calls"][0]["qa_trace"]["started_at"]
    assert result["tool_calls"][0]["qa_trace"]["ended_at"]
    assert backend.market_calls == [("K01B3", "중외5포도당생리식염액", "iqvia", "sales")]


def test_general_view_unavailable_call_records_no_data_trace() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership(set()), enabled=True)

    result = service.answer("C10AA ATC4 시장 규모", compact=False, dual=False)

    call = result["tool_calls"][0]
    assert call["tool"] == "general_view_unavailable"
    assert call["qa_trace"]["status"] == "no_data"
    assert call["qa_trace"]["row_count"] == 0
    assert call["qa_trace"]["started_at"]
    assert call["qa_trace"]["ended_at"]


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


def _iqvia_intent_service() -> GeneralViewService:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "iqvia"),
    )
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(memberships),
        ttl_seconds=300,
    )
    backend = FakeBackend()
    backend.market_map["S01P0"] = replace(
        _market("S01P0", 21_870_000_000.0),
        source="IQVIA NSA",
        period="2026-Q1",
        market_size=91_125_000_000.0,
        brand="아일리아",
        brand_share_pct=24.0,
        brand_rank=1,
        hhi_recent=3188.04,
        market_size_series=(
            ("2025-Q2", 80_000_000_000.0),
            ("2025-Q3", 83_000_000_000.0),
            ("2025-Q4", 87_000_000_000.0),
            ("2026-Q1", 91_125_000_000.0),
        ),
        brand_metric_series=(
            BrandMetricPoint("2025-Q2", 18_000_000_000.0, 22.0, 2),
            BrandMetricPoint("2026-Q1", 21_870_000_000.0, 24.0, 1),
        ),
        top_brands=(
            TopBrand("아일리아", 1, 21_870_000_000.0, 24.0, 21.5, "2025-Q1", "2026-Q1"),
            TopBrand("루센티스", 2, 16_400_000_000.0, 18.0, 8.0, "2025-Q1", "2026-Q1"),
            TopBrand("비오뷰", 3, 10_025_000_000.0, 11.0, -3.5, "2025-Q1", "2026-Q1"),
            TopBrand("바비스모", 4, 8_200_000_000.0, 9.0, 42.0, "2025-Q1", "2026-Q1"),
            TopBrand("아바스틴", 5, 6_375_000_000.0, 7.0, 2.5, "2025-Q1", "2026-Q1"),
        ),
        selected_data_path="direct_mart",
    )
    return GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )


def test_general_view_intents_render_distinct_strategic_control_surfaces() -> None:
    service = _iqvia_intent_service()

    market_size = service.answer("아일리아 시장 규모 알려줘", compact=False, dual=False)
    brand_trend = service.answer("아일리아 매출 알려줘", compact=False, dual=False)
    competition = service.answer("아일리아 경쟁 구도 어때", compact=False, dual=False)
    concentration = service.answer("아일리아 시장 HHI", compact=False, dual=False)

    assert market_size["general_view_contract"]["general_view_intent"] == "MARKET_SIZE_TREND"
    assert brand_trend["general_view_contract"]["general_view_intent"] == "BRAND_TREND"
    assert competition["general_view_contract"]["general_view_intent"] == "COMPETITION_CHANGE"
    assert concentration["general_view_contract"]["general_view_intent"] == "MARKET_CONCENTRATION"

    assert market_size["answer"].count("## 핵심 결과") == 1
    assert "시장 규모" in market_size["answer"]
    assert len(market_size["general_view_contract"]["chart_payloads"]) == 1

    assert brand_trend["answer"].count("## 핵심 결과") == 1
    assert "브랜드 매출" in brand_trend["answer"]
    assert "점유율 24.00%" in brand_trend["answer"]
    assert "순위 1위" in brand_trend["answer"]
    assert brand_trend["general_view_contract"]["chart_payloads"] == []

    assert competition["answer"].count("## 핵심 결과") == 1
    assert "1위 아일리아 (24.00%)" in competition["answer"]
    assert "+2.00%p" in competition["answer"]
    assert competition["general_view_contract"]["chart_payloads"] == []

    assert concentration["answer"].count("## 핵심 결과") == 1
    assert "HHI 3,188.04" in concentration["answer"]
    assert "CR5 69.00%" in concentration["answer"]
    assert concentration["general_view_contract"]["chart_payloads"] == []


def test_general_view_competitor_growth_uses_ranked_mart_rows() -> None:
    service = _iqvia_intent_service()

    result = service.answer("아일리아 경쟁사 성장률 표", compact=False, dual=False)

    answer = result["answer"]
    assert "| 순위 | 브랜드 | 점유율 | 매출 | 성장률(YoY, 2026-Q1 대비 2025-Q1) |" in answer
    assert answer.count("| 1위 | 아일리아 |") == 1
    assert answer.count("| 5위 | 아바스틴 |") == 1
    assert "21.50%" in answer
    assert "ATC4 S01P0 시장 전체 sales" in answer
    assert "전략뷰와 시장 정의·분모가 다릅니다" in answer


def test_general_view_competitor_growth_survives_final_binding() -> None:
    service = _iqvia_intent_service()
    question = "아일리아 경쟁사 성장률 표"
    result = service.answer(question, compact=False, dual=False)

    final = service_app.compute_final_answer(question, result, "general-competitor-binding")

    assert final.text.count("## 일반뷰 (ATC4)") == 1
    assert final.text.count("| 순위 | 브랜드 | 점유율 | 매출 | 성장률(YoY, 2026-Q1 대비 2025-Q1) |") == 1
    assert final.text.count("| 1위 | 아일리아 | 24.00% | 218.70억원 | 21.50% |") == 1
    assert final.text.count("| 5위 | 아바스틴 | 7.00% | 63.75억원 | 2.50% |") == 1
    assert final.trace["numeric_copy_contract"] == {
        "requested_metrics": ["growth"],
        "rendered_metrics": ["growth"],
        "dropped_metrics": [],
        "reason_codes": [],
    }


def test_general_view_competitor_growth_reports_missing_source_exactly() -> None:
    service = _iqvia_intent_service()
    market = service._backend.market_map["S01P0"]  # type: ignore[attr-defined]
    service._backend.market_map["S01P0"] = replace(  # type: ignore[attr-defined]
        market,
        top_brands=tuple(replace(row, growth_pct=None) for row in market.top_brands),
    )

    result = service.answer("아일리아 경쟁사 성장률 표", compact=False, dual=False)

    assert "일반뷰에는 성장률 원천이 없습니다" in result["answer"]


def test_iqvia_general_view_warning_is_in_answer_body() -> None:
    service = _iqvia_intent_service()
    question = "아일리아 시장 HHI"
    result = service.answer(question, compact=False, dual=False)

    final = service_app.compute_final_answer(question, result, "general-view-warning-test")

    assert (
        "이 값은 일반뷰(IQVIA NSA, ATC4 기준)입니다. "
        "전략뷰(UBIST) 값과 분모·기간이 달라 직접 비교할 수 없습니다."
    ) in final.text
    assert final.text.count("## 핵심 결과") == 1


def test_general_view_fast_path_materializes_authorized_market_size_chart() -> None:
    service = _iqvia_intent_service()
    question = "아일리아 시장 규모 알려줘"
    result = service.answer(question, compact=False, dual=False)

    final = service_app.compute_final_answer(question, result, "general-view-intent-test")

    assert len(final.charts) == 1
    assert final.charts[0]["title"] == "시장 규모 추이"
    assert final.charts[0]["labels"] == ["2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"]


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

    assert "2026-04 시장 규모 1,000.00억원" in result["answer"]
    assert "2026-04~2026-04" not in result["answer"]


def test_route_matrix_has_no_human_loop() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership({"리바로"}), enabled=True)

    assert service.route("리바로 ATC4 기준 점유율") is GeneralRoute.GENERAL_ONLY
    assert service.route("리바로 전략뷰 시장 점유율") is GeneralRoute.EXISTING
    assert service.route("ml_006 2025-04 시장규모") is GeneralRoute.EXISTING
    assert service.route("리바로 시장 점유율은?") is GeneralRoute.DUAL
    assert service.route("포도당 대한 시장 점유율은?") is GeneralRoute.GENERAL_ONLY


def test_unknown_brand_in_explicit_strategic_market_stays_on_typed_brand_path() -> None:
    service = GeneralViewService(
        FakeBackend(),
        StrategicMembershipWithExplicitMarket(set()),
        enabled=True,
    )

    assert (
        service.route("고지혈증 시장에서 없는브랜드ABC 점유율")
        is GeneralRoute.EXISTING
    )


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


class GeneralOnlyResolvingMembership(StrategicMembership):
    def resolve(self, question: str, allow_default: bool = False):
        if "아일리아" in question:
            return type(
                "Resolution",
                (),
                {"canonical_brand": "아일리아", "market_id": None, "market_ids": ()},
            )()
        return super().resolve(question, allow_default=allow_default)


@pytest.mark.parametrize(
    "question",
    (
        "아일리아 매출 알려줘",
        "아일리아 최근 추이",
        "아일리아 경쟁사 성장률 표",
        "아일리아 경쟁 순위 기타 포함 제품 목록",
        "아일리아 시장 브랜드",
        "아일리아 시장 기타 브랜드",
    ),
)
def test_general_only_resolved_brand_routes_to_general_view(question: str) -> None:
    service = GeneralViewService(
        FakeBackend(),
        GeneralOnlyResolvingMembership({"리바로"}),
        enabled=True,
    )

    assert service.route(question) is GeneralRoute.GENERAL_ONLY


def test_general_only_competition_routes_to_direct_atc4_general_view() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(memberships), ttl_seconds=300
    )
    backend = FakeBackend()
    backend.market_map["S01P0"] = replace(
        _market("S01P0", 8_000_000_000.0),
        brand="아일리아",
        selected_data_path="direct_mart",
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership({"리바로"}),
        enabled=True,
        general_membership=cache,
    )

    question = "아일리아 경쟁 약물 현황"
    assert service.route(question) is GeneralRoute.GENERAL_ONLY

    result = service.answer(question, compact=False, dual=False)
    contract = result["general_view_contract"]
    assert contract["atc4_code"] == "S01P0"
    assert contract["market_basis"] == "ATC4"
    assert contract["selected_data_path"] == "direct_mart"
    assert contract["share_denominator"] == "ATC4 S01P0 시장 전체 sales"
    assert result["tool_calls"][0]["tool"] == "general_view_dynamic_market"
    assert result["tool_calls"][0]["source"] == "jw-market-direct-mart"
    assert "일반뷰 (ATC4)" in result["answer"]
    assert "전략시장 정의에 포함되지 않아" not in result["answer"]

    rendered = enforce_general_view_contract("", contract)
    assert "기준: 일반뷰 (ATC4 S01P0)" in rendered
    assert "전략뷰와 일반뷰는 시장 구성과 분모가 달라 수치를 직접 비교할 수 없습니다" in rendered


def test_strategic_brand_competition_does_not_leak_to_general_view() -> None:
    service = GeneralViewService(
        FakeBackend(),
        StrategicMembership({"리바로"}),
        enabled=True,
    )

    assert service.route("리바로 경쟁 약물 현황") is GeneralRoute.EXISTING


def test_general_only_brand_hhi_routes_to_general_view() -> None:
    service = GeneralViewService(
        FakeBackend(),
        GeneralOnlyResolvingMembership({"리바로"}),
        enabled=True,
    )

    assert service.route("아일리아 시장 HHI") is GeneralRoute.GENERAL_ONLY


@pytest.mark.parametrize("metric", ("CR5", "집중도"))
def test_general_only_brand_unverified_concentration_metrics_keep_existing_route(metric: str) -> None:
    service = GeneralViewService(
        FakeBackend(),
        GeneralOnlyResolvingMembership({"리바로"}),
        enabled=True,
    )

    assert service.route(f"아일리아 시장 {metric}") is GeneralRoute.EXISTING


def test_general_only_brand_hhi_is_rendered_from_one_atc4_without_aggregation() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["S01P0"] = replace(
        _market("S01P0", 8_000_000_000.0),
        brand="아일리아",
        hhi_recent=1234.5678,
        selected_data_path="direct_mart",
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 HHI", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["hhi_recent"] == pytest.approx(1234.5678)
    assert contract["atc4_code"] == "S01P0"
    assert "일반뷰 (ATC4)" in result["answer"]
    assert "ATC4 S01P0" in result["answer"]
    assert "HHI 1,234.57" in result["answer"]
    assert "market_id" not in result["answer"]


def test_general_only_brand_hhi_splits_two_atc4_without_aggregation() -> None:
    memberships = (
        GeneralBrandMembership("복합브랜드", "복합브랜드", "A10A1", "첫 번째 시장", "ubist"),
        GeneralBrandMembership("복합브랜드", "복합브랜드", "A10B2", "두 번째 시장", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["A10A1"] = replace(
        _market("A10A1", 10.0),
        brand="복합브랜드",
        hhi_recent=111.0,
    )
    backend.market_map["A10B2"] = replace(
        _market("A10B2", 20.0),
        brand="복합브랜드",
        hhi_recent=222.0,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("복합브랜드 시장 HHI", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["atc4_codes"] == ["A10A1", "A10B2"]
    assert [section["hhi_recent"] for section in contract["atc4_sections"]] == [111.0, 222.0]
    assert "ATC4 A10A1" in result["answer"]
    assert "ATC4 A10B2" in result["answer"]
    assert "HHI 111.00" in result["answer"]
    assert "HHI 222.00" in result["answer"]
    assert "합계" not in result["answer"]
    assert "평균" not in result["answer"]


def test_general_only_brand_hhi_lists_candidates_without_querying_when_atc4_count_exceeds_limit() -> None:
    memberships = tuple(
        GeneralBrandMembership("다중브랜드", "다중브랜드", f"A10A{index}", f"ATC4 {index}", "ubist")
        for index in range(1, 6)
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("다중브랜드 시장 HHI", compact=False, dual=False)

    assert result["general_view_contract"]["unavailable"] is True
    assert "4개를 초과" in result["answer"]
    assert "ATC4를 지정" in result["answer"]
    assert backend.market_calls == []


def test_general_membership_hit_routes_unknown_strategic_brand_to_general_view() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (
                GeneralBrandMembership(
                    "카나브패밀리",
                    "카나브패밀리",
                    "C09C0",
                    "혈압강하제",
                    "ubist",
                ),
            )
        ),
        ttl_seconds=300,
    )
    service = GeneralViewService(
        FakeBackend(),
        StrategicMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    assert service.route("카나브패밀리 실적 어때?") is GeneralRoute.GENERAL_ONLY


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


@pytest.mark.parametrize("question", ("포도당 시장 규모 추이", "포도당 시장 규모 비교"))
def test_general_only_market_scope_keeps_analytic_questions_on_general_path(question: str) -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership(set()), enabled=True)

    assert service.route(question) is GeneralRoute.GENERAL_ONLY


def test_general_only_brand_news_keeps_the_existing_external_path() -> None:
    service = GeneralViewService(FakeBackend(), StrategicMembership(set()), enabled=True)

    assert service.route("포도당이 속한 시장 최신 뉴스") is GeneralRoute.EXISTING


def test_general_member_listing_reports_other_members_and_total_population() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    all_members = tuple(
        replace(market.top_brands[0], brand=f"브랜드{index}", rank=index)
        for index in range(1, 9)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer(
        "아일리아 경쟁 순위 '기타'에 포함된 제품 목록",
        compact=False,
        dual=False,
    )

    contract = result["general_view_contract"]
    assert result["decomposition"] == [{"intent": "market_members", "view_type": "general_view"}]
    assert result["tool_calls"][0]["tool"] == "get_market_members"
    assert contract["total_brands_in_market"] == 8
    assert contract["displayed_brand_count"] == 3
    assert contract["member_brands"] == ["브랜드6", "브랜드7", "브랜드8"]
    assert contract["other_member_count"] == 3
    assert "기타 3개 중 3개 표시" in result["answer"]
    assert "브랜드6" in result["answer"]


def test_general_member_listing_uses_existing_bounded_market_member_contract() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    all_members = tuple(
        replace(
            market.top_brands[0],
            brand=f"브랜드{index}",
            rank=index,
            share_pct=float(26 - index),
        )
        for index in range(1, 31)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 브랜드", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert result["tool_calls"][0]["tool"] == "get_market_members"
    assert contract["total_brands_in_market"] == 30
    assert contract["displayed_brand_count"] == 20
    assert contract["member_brands"] == [f"브랜드{index}" for index in range(1, 21)]
    assert "총 30개 중 20개 표시" in result["answer"]
    assert "브랜드21" not in result["answer"]


def test_general_other_listing_starts_after_top_five_and_reports_share_total() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    shares = (51.38, 19.17, 9.60, 6.43, 5.05, 3.50, 2.25, 1.12, 0.87, 0.63)
    all_members = tuple(
        replace(
            market.top_brands[0],
            brand=f"브랜드{index}",
            rank=index,
            share_pct=share,
        )
        for index, share in enumerate(shares, 1)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 기타 브랜드", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["member_brands"] == [f"브랜드{index}" for index in range(6, 11)]
    assert contract["other_member_count"] == 5
    assert contract["other_total_share_pct"] == pytest.approx(8.37)
    assert "기타 5개 중 5개 표시" in result["answer"]
    assert "기타 합계 점유율" in result["answer"]
    assert "8.37%" in result["answer"]
    assert "| 6 | 브랜드6 |" in result["answer"]


@pytest.mark.parametrize(
    "question",
    (
        "아일리아 시장 기타 브랜드",
        "기타에 포함된 제품 목록",
        "상위 5개 외 나머지",
    ),
)
def test_general_other_member_phrases_are_recognized(question: str) -> None:
    assert asks_market_members(question) is True


def test_general_other_listing_reports_none_when_market_has_only_top_five() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        member_brands=market.top_brands,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 기타 브랜드", compact=False, dual=False)

    assert result["general_view_contract"]["other_member_count"] == 0
    assert "상위 5개 외 기타 브랜드가 없습니다" in result["answer"]


def test_general_member_full_listing_request_stays_bounded() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    all_members = tuple(
        replace(market.top_brands[0], brand=f"브랜드{index}", rank=index)
        for index in range(1, 101)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 브랜드 전부 나열해줘", compact=False, dual=False)

    assert result["general_view_contract"]["displayed_brand_count"] == 100
    assert "전체 100개 · 전체 요청 · 표시 100개" in result["answer"]
    assert "브랜드100" in result["answer"]
    assert "표시 상한" not in result["answer"]


def test_exact_strategic_member_question_keeps_strategic_route_priority() -> None:
    service = GeneralViewService(
        FakeBackend(),
        StrategicMembershipWithExplicitMarket({"리바로"}),
        enabled=True,
    )

    assert service.route("고지혈증 시장에 어떤 브랜드들이 있어?") is GeneralRoute.EXISTING


def test_general_member_listing_rejects_explicit_historical_period_without_latest_substitution() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["S01P0"] = replace(
        _market("S01P0", 10.0),
        brand="아일리아",
        period="2026-05",
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer(
        "아일리아 2024년 경쟁 순위 기타 포함 제품 목록",
        compact=False,
        dual=False,
    )

    assert result["tool_calls"][0]["tool"] == "general_view_unavailable"
    assert "2024" in result["answer"]
    assert "2026-05" not in result["answer"]


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


@pytest.mark.parametrize(
    ("question", "requested", "legacy_applied", "legacy_capped"),
    (
        ("고지혈증 시장 브랜드 50개", 50, 20, True),
        ("고지혈증 시장 상위 50개", 50, 20, True),
        ("고지혈증 시장 20개만", 20, 20, False),
        ("고지혈증 시장 50개 알려줘", 50, 20, True),
        ("고지혈증 시장 top 50", 50, 20, True),
        ("고지혈증 시장 상위 10개 브랜드", 10, 10, False),
        ("고지혈증 시장 브랜드 999개", 999, 20, True),
        ("고지혈증 시장 브랜드 0개", 0, 20, False),
        ("고지혈증 시장 브랜드 -5개", -5, 20, False),
    ),
)
def test_market_member_count_phrases_are_separate_bounded_display_limits(
    question: str,
    requested: int,
    legacy_applied: int,
    legacy_capped: bool,
) -> None:
    request = market_scope_intent.requested_market_member_limit(question)

    assert asks_market_members(question) is True
    assert request.requested == requested
    assert request.applied == (requested if requested > 0 else 20)
    assert request.capped is False
    assert request.all_requested is False
    assert legacy_applied > 0
    assert isinstance(legacy_capped, bool)


@pytest.mark.parametrize(
    "question",
    (
        "고지혈증 시장 브랜드 전부",
        "고지혈증 시장 구성 브랜드 전체",
    ),
)
def test_market_member_all_phrases_request_the_full_population(question: str) -> None:
    request = market_scope_intent.requested_market_member_limit(question)

    assert asks_market_members(question) is True
    assert request.requested is None
    assert request.applied is None
    assert request.capped is False
    assert request.all_requested is True


def test_number_in_market_name_is_not_treated_as_a_display_limit() -> None:
    request = market_scope_intent.requested_market_member_limit("제2형 당뇨 시장에 어떤 브랜드들이 있어?")

    assert request.requested is None
    assert request.applied == 20
    assert request.capped is False


def test_ranked_market_share_metric_is_not_treated_as_member_listing() -> None:
    assert asks_market_members("리바로 시장 상위 3개 브랜드 점유율") is False


def test_general_member_request_applies_requested_limit_below_cap() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    all_members = tuple(
        replace(market.top_brands[0], brand=f"브랜드{index}", rank=index)
        for index in range(1, 31)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 상위 10개 브랜드", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["displayed_brand_count"] == 10
    assert contract["requested_limit"] == 10
    assert contract["limit"] == 10
    assert "전체 30개 · 요청 10개 · 표시 10개" in result["answer"]
    assert "브랜드11" not in result["answer"]


def test_general_member_request_discloses_the_twenty_row_cap() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    all_members = tuple(
        replace(market.top_brands[0], brand=f"브랜드{index}", rank=index)
        for index in range(1, 101)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )

    result = service.answer("아일리아 시장 브랜드 50개", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["displayed_brand_count"] == 50
    assert contract["requested_limit"] == 50
    assert contract["limit_capped"] is False
    assert "전체 100개 · 요청 50개 · 표시 50개" in result["answer"]
    assert "표시 상한" not in result["answer"]


def test_general_member_count_matrix_returns_requested_or_available_rows() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    market = _market("S01P0", 10.0)
    all_members = tuple(
        replace(market.top_brands[0], brand=f"브랜드{index}", rank=index)
        for index in range(1, 556)
    )
    backend.market_map["S01P0"] = replace(
        market,
        brand="아일리아",
        top_brands=all_members[:5],
        member_brands=all_members,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
    )
    cases = (
        ("아일리아 시장 브랜드 50개", 50),
        ("아일리아 시장 브랜드 상위 50개", 50),
        ("아일리아 시장 브랜드 top 50", 50),
        ("아일리아 시장 브랜드 20개만", 20),
        ("아일리아 시장 브랜드 100개", 100),
        ("아일리아 시장 브랜드 555개", 555),
        ("아일리아 시장 브랜드 전부", 555),
        ("아일리아 시장 브랜드 전체", 555),
        ("아일리아 시장 브랜드 0개", 20),
        ("아일리아 시장 브랜드 -5개", 20),
        ("아일리아 시장에 어떤 브랜드들이 있어?", 20),
    )

    for question, expected_count in cases:
        result = service.answer(question, compact=False, dual=False)
        contract = result["general_view_contract"]

        assert contract["displayed_brand_count"] == expected_count, question
        assert len(contract["member_brands"]) == expected_count, question
        if question in {
            "아일리아 시장 브랜드 0개",
            "아일리아 시장 브랜드 -5개",
            "아일리아 시장에 어떤 브랜드들이 있어?",
        }:
            assert f"총 555개 중 {expected_count}개 표시" in result["answer"], question
        elif "전부" in question or "전체" in question:
            assert f"전체 555개 · 전체 요청 · 표시 {expected_count}개" in result["answer"], question
        else:
            assert f"전체 555개 · 요청 {expected_count}개 · 표시 {expected_count}개" in result["answer"], question
        assert "표시 상한" not in result["answer"], question


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


def test_multi_atc_contract_appends_each_scope_label_idempotently() -> None:
    sections = [
        {
            "atc4_code": code,
            "source": "UBIST",
            "measure": "sales",
            "period": "2026-04",
        }
        for code in ("C10A1", "C10C")
    ]
    contract = {
        "mode": "general_only",
        "view_type": "general_view",
        "market_basis": "ATC4",
        "split_by": "ATC4",
        "atc4_codes": ["C10A1", "C10C"],
        "atc4_sections": sections,
        "section_markdown": "## 일반뷰 (ATC4별 분리)\n\n두 섹션",
    }

    once = enforce_general_view_contract("", contract)
    twice = enforce_general_view_contract(once, contract)

    assert once == twice
    assert once.count("기준: 일반뷰 (ATC4 C10A1)") == 1
    assert once.count("기준: 일반뷰 (ATC4 C10C)") == 1


def test_general_only_competitor_contract_replaces_partially_filtered_table() -> None:
    section = (
        "## 일반뷰 (ATC4)\n\n"
        "| 순위 | 브랜드 | 점유율 | 매출 | 성장률(YoY, 2026-Q1 대비 2025-Q1) |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 1위 | 아일리아 | 51.38% | 218.67억원 | 16.26% |"
    )
    contract = {
        "mode": "general_only",
        "view_type": "general_view",
        "atc4_code": "S01P0",
        "source": "IQVIA NSA",
        "measure": "sales",
        "period": "2026-Q1",
        "section_markdown": section,
        "competitor_rows": [{"brand": "아일리아"}],
    }
    partially_filtered = (
        "## 일반뷰 (ATC4)\n\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 1위 | 아일리아 | 51.38% | 218.67억원 | 16.26% |"
    )

    answer = enforce_general_view_contract(partially_filtered, contract)

    assert answer.count("## 일반뷰 (ATC4)") == 1
    assert answer.count("| 순위 | 브랜드 | 점유율 | 매출 | 성장률") == 1
    assert "| 1위 | 아일리아 | 51.38% | 218.67억원 | 16.26% |" in answer


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


def _canonical_hyperlipidemia_service(
    *,
    exact_market: tuple[str, str] | None = ("ml_006", "고지혈증 치료제 시장"),
    strategic_membership: StrategicMembership | None = None,
) -> GeneralViewService:
    backend = FakeBackend()
    backend.market_map["C10A1"] = replace(_market("C10A1", 8_000_000_000), hhi_recent=250.0)
    backend.market_map["C10C"] = replace(_market("C10C", 4_000_000_000), hhi_recent=300.0)
    definition = StrategicMarketDefinition("ml_006", "ubist", ("C10A1", "C10C"))
    reader = StaticStrategicMarketDefinitionReader(
        {"ml_006": definition},
        {
            "고지혈증": exact_market,
            "고지혈증 치료제": exact_market,
        },
    )
    return GeneralViewService(
        backend,
        strategic_membership or StrategicMembershipWithExplicitMarket(set()),
        enabled=True,
        market_definition_reader=reader,
    )


def test_bare_general_view_hhi_resolves_exact_canonical_market_and_persists_slot() -> None:
    service = _canonical_hyperlipidemia_service()

    result = service.answer("고지혈증 일반뷰 HHI", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["atc4_codes"] == ["C10A1", "C10C"]
    assert contract["market_id"] == "ml_006"
    assert contract["market_name"] == "고지혈증 치료제 시장"
    assert all(section["market_id"] == "ml_006" for section in contract["atc4_sections"])
    slots = extract_conversation_slots(result)
    assert slots.market == "ml_006"
    assert slots.market_definition == "고지혈증 치료제 시장"


def test_bare_general_view_hhi_reuses_unique_public_market_alias_when_catalog_name_is_brand_group() -> None:
    service = _canonical_hyperlipidemia_service(exact_market=None)

    result = service.answer("고지혈증 일반뷰 HHI", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["atc4_codes"] == ["C10A1", "C10C"]
    assert contract["market_id"] == "ml_006"
    assert contract["market_name"] == "고지혈증 치료제 시장"
    assert contract.get("unavailable", False) is False


def test_bare_general_view_hhi_slot_resolves_followup_but_not_standalone_question() -> None:
    service = _canonical_hyperlipidemia_service()
    first = service.answer("고지혈증 일반뷰 HHI", compact=False, dual=False)
    previous = ConversationTurn(
        question="고지혈증 일반뷰 HHI",
        answer=first["answer"],
        slots=extract_conversation_slots(first),
    )

    chained = resolve_anaphora("이 시장 HHI", previous)
    standalone = resolve_anaphora("이 시장 HHI", None)
    chained_result = service.answer(chained.resolved_question, compact=False, dual=False)

    assert chained.resolved_question == "고지혈증 치료제 시장 일반뷰 HHI"
    assert chained.unresolved_reference is False
    assert chained.reference_status.value == "resolved"
    assert service.route(chained.resolved_question) is GeneralRoute.GENERAL_ONLY
    assert chained_result["general_view_contract"]["atc4_codes"] == ["C10A1", "C10C"]
    assert [
        section["hhi_recent"] for section in chained_result["general_view_contract"]["atc4_sections"]
    ] == [250.0, 300.0]
    assert standalone.unresolved_reference is True


def test_bare_general_view_hhi_fails_closed_when_canonical_market_is_ambiguous() -> None:
    service = _canonical_hyperlipidemia_service(
        exact_market=None,
        strategic_membership=StrategicMembership(set()),
    )
    backend = service._backend
    assert isinstance(backend, FakeBackend)
    backend.candidate_map[("고지혈증", "ubist")] = (AtcCandidate("C10A1", "스타틴류"),)

    result = service.answer("고지혈증 일반뷰 HHI", compact=False, dual=False)

    assert result["general_view_contract"]["unavailable"] is True
    assert result["router_diagnostics"]["candidate_atc4_codes"] == []
    assert backend.market_calls == []
    assert extract_conversation_slots(result).market is None


def test_exact_market_lookup_does_not_capture_known_general_view_brand() -> None:
    memberships = (
        GeneralBrandMembership("아일리아", "아일리아", "S01P0", "안과용제", "ubist"),
    )
    cache = TtlGeneralMembershipCache(StaticGeneralMembershipReader(memberships), ttl_seconds=300)
    backend = FakeBackend()
    backend.market_map["S01P0"] = replace(
        _market("S01P0", 8_000_000_000.0),
        brand="아일리아",
        hhi_recent=1234.5678,
    )
    service = GeneralViewService(
        backend,
        GeneralOnlyResolvingMembership(set()),
        enabled=True,
        general_membership=cache,
        market_definition_reader=StaticStrategicMarketDefinitionReader({}, {}),
    )

    result = service.answer("아일리아 일반뷰 HHI", compact=False, dual=False)

    assert result["general_view_contract"]["hhi_recent"] == pytest.approx(1234.5678)
    assert backend.market_calls == [("S01P0", "아일리아", "ubist", "sales")]


def test_general_view_exact_market_lookup_does_not_capture_hira_patient_question() -> None:
    service = _canonical_hyperlipidemia_service()

    assert service.route("고지혈증 환자수") is GeneralRoute.EXISTING
