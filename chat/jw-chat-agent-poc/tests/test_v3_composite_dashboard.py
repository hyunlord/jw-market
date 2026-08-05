from __future__ import annotations

import copy
import json

import pytest

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    BrandOutsideCompositeScopeError,
    InvalidMarketLabelError,
    MarketScopeKind,
)
from jw_chat_agent_poc.tool_use.market_scope_execution import ScopeResolver
from jw_chat_agent_poc.tools.general_view_backend import (
    GeneralViewBackend,
    GeneralViewBackendError,
    canonical_hhi,
    parse_composite_market_response,
)
from jw_chat_agent_poc.tool_use.v3_execution_contracts import MarketMetricFact, V3EvidenceBundle
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import build_fusion_messages
from jw_chat_agent_poc.tool_use.v3_fusion import validate_fusion_answer
from jw_chat_agent_poc.tool_use.v3_fusion_contracts import GeneratedFusionAnswer, GeneratedFusionClaim
from v3_market_scope_fakes import FakeGeneralMembership, FakeStrategicLayer, make_backend


def _payload() -> dict[str, object]:
    return {
        "status": "SUCCESS",
        "result": {
            "unit_label": "KRW",
            "market_meta": {
                "market_definition_label": "ATC4 S01P0, A02A2",
                "brand_list": ["아일리아", "루센티스", "제로브랜드"],
                "filters": {
                    "view": "general",
                    "atc4": ["S01P0", "A02A2"],
                    "analysis_level": {
                        "mfr": ["제조사A"],
                        "nhi": ["급여"],
                    },
                    "channel_axis": {"audit_code": ["RX"]},
                    "focus_brand_key": "아일리아",
                    "source": "iqvia_nsa",
                    "measure": "sales",
                },
            },
            "data": {
                "hhi_calculation_input": {
                    "period": "2026-Q1",
                    "market_total": 100.0,
                    "brand_values": [
                        {"brand": "아일리아", "value": 50.0},
                        {"brand": "루센티스", "value": 30.0},
                        {"brand": "제로브랜드", "value": 0.0},
                        {"brand": "기타", "value": 20.0},
                    ],
                },
                "kpi": {
                    "market_size_recent": 100.0,
                    "target_brand": "아일리아",
                    "target_rank": 1,
                    "target_share_pct": 50.0,
                    "target_brand_sales": 50.0,
                },
                "market_size_series": [{"period": "2026-Q1", "value": 100.0}],
                "ei_ms_matrix": {
                    "data": [
                        {
                            "brand": "아일리아",
                            "rank": 1,
                            "value_recent": 50.0,
                            "share_pct": 50.0,
                        },
                        {
                            "brand": "루센티스",
                            "rank": 2,
                            "value_recent": 30.0,
                            "share_pct": 30.0,
                        },
                    ]
                },
                "growth_contribution": {
                    "period_start": "2025-Q1",
                    "period_end": "2026-Q1",
                    "market_growth": 10.0,
                    "by_brand": {
                        "top_contributors": [
                            {
                                "brand": "아일리아",
                                "contribution": 6.0,
                                "contribution_pct": 60.0,
                            }
                        ]
                    },
                },
            },
        },
    }


def test_resolver_accepts_only_observed_iqvia_composite_axes() -> None:
    resolver = ScopeResolver(
        strategic_memberships=FakeStrategicLayer().brand_memberships,
        general_membership=FakeGeneralMembership(),
    )

    resolution = resolver.resolve(
        {
            "brand": "아일리아",
            "source": "iqvia",
            "scope": {
                "kind": "general_composite",
                "atc4": ["S01P0", "A02A2"],
                "filters": {
                    "mfr_name_kor": ["제조사A"],
                    "nhi_type": ["급여"],
                    "audit_code": ["RX"],
                },
            },
        }
    )

    assert resolution.scope.kind is MarketScopeKind.GENERAL_COMPOSITE
    assert resolution.scope.atc4 == ("S01P0", "A02A2")


def test_resolver_accepts_only_portal_visible_ubist_composite_axes() -> None:
    resolver = ScopeResolver(
        strategic_memberships=FakeStrategicLayer().brand_memberships,
        general_membership=FakeGeneralMembership(),
    )

    resolution = resolver.resolve(
        {
            "brand": "리바로",
            "source": "ubist",
            "scope": {
                "kind": "general_composite",
                "atc4": ["C10A1", "C10B1"],
                "filters": {
                    "seller": ["제이더블유중외제약"],
                    "molecule": ["PITAVASTATIN"],
                    "specialty": ["내과"],
                    "facility": ["상급종합병원"],
                },
            },
        }
    )

    assert resolution.scope.kind is MarketScopeKind.GENERAL_COMPOSITE
    assert resolution.scope.atc4 == ("C10A1", "C10B1")


@pytest.mark.parametrize("field", ["molecule", "unknown_axis", "facility", "atc3"])
def test_resolver_rejects_disabled_unknown_or_cross_source_axes(field: str) -> None:
    resolver = ScopeResolver(
        strategic_memberships=FakeStrategicLayer().brand_memberships,
        general_membership=FakeGeneralMembership(),
    )

    with pytest.raises(InvalidMarketLabelError):
        resolver.resolve(
            {
                "brand": "아일리아",
                "source": "iqvia",
                "scope": {
                    "kind": "general_composite",
                    "atc4": ["S01P0"],
                    "filters": {field: ["값"]},
                },
            }
        )


def test_composite_backend_sends_verified_dynamic_market_shape() -> None:
    class Session:
        def __init__(self) -> None:
            self.body: dict[str, object] | None = None

        def request(self, method: str, url: str, **kwargs: object):
            self.body = kwargs["json"]  # type: ignore[assignment]

            class Response:
                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict[str, object]:
                    return _payload()

            return Response()

    session = Session()
    backend = GeneralViewBackend(base_url="http://example", session=session)  # type: ignore[arg-type]

    market = backend.composite_market(
        atc4=("S01P0", "A02A2"),
        filters=(
            ("audit_code", ("RX",)),
            ("mfr_name_kor", ("제조사A",)),
            ("nhi_type", ("급여",)),
        ),
        brand="아일리아",
        source="iqvia",
        measure="sales",
    )

    assert session.body == {
        "view": "general",
        "filters": {
            "atc4": ["S01P0", "A02A2"],
            "focus_brand_key": "아일리아",
            "analysis_level": {
                "iqvia": {
                    "audit_code": ["RX"],
                    "mfr_name_kor": ["제조사A"],
                    "nhi_type": ["급여"],
                }
            },
        },
        "source": "iqvia",
        "measure": "sales",
    }
    assert market.market_basis == "ATC4 composite"
    assert market.atc4_codes == ("S01P0", "A02A2")
    assert market.hhi_recent == 3800.0
    assert market.hhi_period == "2026-Q1"
    assert market.member_population == (
        "아일리아",
        "루센티스",
        "제로브랜드",
        "기타",
    )
    assert tuple(row.brand for row in market.active_members) == (
        "아일리아",
        "루센티스",
        "기타",
    )
    assert tuple(row.brand for row in market.display_members) == ("아일리아", "루센티스")
    assert tuple(table["name"] for table in market.dashboard_tables) == (
        "시장 KPI",
        "시장 규모 추이",
        "브랜드 순위",
        "성장 기여",
    )
    assert market.dashboard_tables[0]["rows"] == (
        ("시장 규모", 100.0, "KRW", "2026-Q1"),
        ("HHI", 3800.0, "index", "2026-Q1"),
        ("아일리아 매출", 50.0, "KRW", "2026-Q1"),
        ("아일리아 점유율", 50.0, "%", "2026-Q1"),
        ("아일리아 순위", 1, "rank", "2026-Q1"),
    )
    assert market.dashboard_tables[1]["rows"] == (("2026-Q1", 100.0, "KRW"),)
    assert market.dashboard_tables[2]["columns"] == (
        "순위",
        "브랜드",
        "최근 값",
        "점유율(%)",
    )


def test_composite_hhi_uses_raw_values_without_intermediate_rounding() -> None:
    values = (44.123456789, 31.234567891, 24.64197532)

    assert canonical_hhi(values) == sum((value / sum(values) * 100) ** 2 for value in values)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            (21867326960.0, 8159719897.0, 4084678829.0, 2736900000.0, 2149659065.0, 1328074813.0, 1302801381.0, 509040000.0, 421363416.0, 0.0),
            3188.0404,
        ),
        (
            (331288000.0, 312464211.0, 254674530.0, 33388208.0, 12989610.0, 5433180.0, 2199456.0, 40905.0, 0.0, 0.0, 0.0, 0.0),
            3015.4125,
        ),
        (
            (13638966609.0, 3286937430.0, 1108332267.0, 307962456.0, 217482949.0, 87169838.0, 52243680.0, 21901275.0, 5460000.0, 0.0, 0.0, 0.0, 0.0),
            5652.0659,
        ),
    ],
)
def test_canonical_hhi_matches_three_preserved_mart_inputs(
    values: tuple[float, ...],
    expected: float,
) -> None:
    assert round(canonical_hhi(values) or 0.0, 4) == expected


def test_composite_parser_rejects_upstream_hhi_that_differs_from_raw_values() -> None:
    payload = copy.deepcopy(_payload())
    payload["result"]["data"]["hhi_calculation_input"]["hhi_raw"] = 3799.9999

    with pytest.raises(GeneralViewBackendError, match="HHI input value mismatch"):
        parse_composite_market_response(
            payload,
            requested_atc4=("S01P0", "A02A2"),
            requested_filters=(
                ("audit_code", ("RX",)),
                ("mfr_name_kor", ("제조사A",)),
                ("nhi_type", ("급여",)),
            ),
            requested_source="iqvia",
            requested_measure="sales",
            requested_brand="아일리아",
        )


def test_composite_parser_preserves_explicit_zero_market_size() -> None:
    payload = copy.deepcopy(_payload())
    payload["result"]["data"]["kpi"]["market_size_recent"] = 0.0

    market = parse_composite_market_response(
        payload,
        requested_atc4=("S01P0", "A02A2"),
        requested_filters=(
            ("audit_code", ("RX",)),
            ("mfr_name_kor", ("제조사A",)),
            ("nhi_type", ("급여",)),
        ),
        requested_source="iqvia",
        requested_measure="sales",
        requested_brand="아일리아",
    )

    assert market.market_size == 0.0


def test_composite_population_uses_complete_hhi_input_not_truncated_meta_list() -> None:
    payload = copy.deepcopy(_payload())
    payload["result"]["market_meta"]["brand_list"] = ["아일리아"]

    market = parse_composite_market_response(
        payload,
        requested_atc4=("S01P0", "A02A2"),
        requested_filters=(
            ("audit_code", ("RX",)),
            ("mfr_name_kor", ("제조사A",)),
            ("nhi_type", ("급여",)),
        ),
        requested_source="iqvia",
        requested_measure="sales",
        requested_brand="루센티스",
    )

    assert market.member_population == (
        "아일리아",
        "루센티스",
        "제로브랜드",
        "기타",
    )


def test_fusion_prompt_requests_markdown_tables_without_bypassing_claim_evidence() -> None:
    fact = MarketMetricFact(
        evidence_id="v3-shadow:market.get_hhi:0123456789abcdef",
        tool_name="market.get_hhi",
        arguments={},
        raw_result={
            "render_data": {
                "dashboard_tables": [
                    {
                        "name": "브랜드 순위",
                        "columns": ["순위", "브랜드", "점유율(%)"],
                        "rows": [[1, "아일리아", 50.0]],
                    }
                ]
            }
        },
        missing_required_fields=(),
        entity="아일리아",
        metric="hhi",
        period="2026-Q1",
        unit="index",
        view="general",
        market="S01P0,A02A2",
    )

    messages = build_fusion_messages(
        "복합 시장 원인분석을 표로 보여줘",
        V3EvidenceBundle(
            status="ok",
            facts=(fact,),
            failures=(),
            deferred=(),
            executions=(),
            original_call_count=1,
            executed_call_count=1,
            deduplicated_call_count=0,
        ),
    )
    payload = json.loads(messages[1]["content"])

    assert "GitHub-flavored Markdown" in messages[0]["content"]
    assert payload["evidence"][0]["raw_result"]["render_data"]["dashboard_tables"]

    accepted = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="| 순위 | 브랜드 | 점유율(%) |\n|---:|---|---:|\n| 1 | 아일리아 | 50.0 |",
                    evidence_ids=(fact.evidence_id,),
                ),
            ),
        ),
        V3EvidenceBundle(
            status="ok",
            facts=(fact,),
            failures=(),
            deferred=(),
            executions=(),
            original_call_count=1,
            executed_call_count=1,
            deduplicated_call_count=0,
        ),
    )
    rejected = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="| 순위 | 브랜드 | 점유율(%) |\n|---:|---|---:|\n| 1 | 아일리아 | 51.0 |",
                    evidence_ids=(fact.evidence_id,),
                ),
            ),
        ),
        V3EvidenceBundle(
            status="ok",
            facts=(fact,),
            failures=(),
            deferred=(),
            executions=(),
            original_call_count=1,
            executed_call_count=1,
            deduplicated_call_count=0,
        ),
    )

    assert len(accepted.answer.claims) == 1
    assert len(rejected.answer.claims) == 0
    assert rejected.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"


def test_composite_parser_rejects_brand_outside_scope() -> None:
    with pytest.raises(BrandOutsideCompositeScopeError):
        parse_composite_market_response(
            _payload(),
            requested_atc4=("S01P0", "A02A2"),
            requested_filters=(
                ("audit_code", ("RX",)),
                ("mfr_name_kor", ("제조사A",)),
                ("nhi_type", ("급여",)),
            ),
            requested_source="iqvia",
            requested_measure="sales",
            requested_brand="없는브랜드",
        )


def test_composite_parser_accepts_full_population_brand_outside_display_top_n() -> None:
    market = parse_composite_market_response(
        _payload(),
        requested_atc4=("S01P0", "A02A2"),
        requested_filters=(
            ("audit_code", ("RX",)),
            ("mfr_name_kor", ("제조사A",)),
            ("nhi_type", ("급여",)),
        ),
        requested_source="iqvia",
        requested_measure="sales",
        requested_brand="제로브랜드",
    )

    assert market.brand == "제로브랜드"
    assert market.brand_value == 0.0
    assert market.brand_share_pct == 0.0
    assert market.brand_rank == 4


def test_composite_scope_executes_in_shadow_catalog_without_fallback_block() -> None:
    backend, _strategic, _general = make_backend()

    result = backend.execute_catalog_tool(
        "market.get_hhi",
        {
            "brand": "아일리아",
            "source": "iqvia",
            "scope": {
                "kind": "general_composite",
                "atc4": ["S01P0", "A02A2"],
                "filters": {"audit_code": ["RX"]},
            },
        },
    )

    assert result["render_data"]["value"] == 3188.0404
    assert result["render_data"]["scope_trace"]["scope_kind"] == "general_composite"
    assert result["render_data"]["selected_data_path"] == "dynamic_market_composite"
    assert result["render_data"]["dashboard_tables"]
