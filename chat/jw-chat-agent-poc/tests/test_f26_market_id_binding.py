"""F26 RC1 market-identity binding regression coverage."""

from __future__ import annotations

from typing import TypedDict

from jw_chat_agent_poc.orchestrator.provenance import (
    EvidenceFact,
    evidence_from_calls,
    evidence_markdown,
)
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.service import evidence_binding
from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate
from jw_chat_agent_poc.service.evidence_binding_rules import fact_scope, scope_matches


class _MarketRenderData(TypedDict):
    brand: str
    market_id: str
    measure: str
    sales_억원: float
    period: str
    source: str
    view_type: str


class _MarketCall(TypedDict):
    tool: str
    render_data: _MarketRenderData


class _NormalizedArgs(TypedDict):
    market_id: str
    brand: str


class _ProposedCall(TypedDict):
    normalized_args: _NormalizedArgs


class _RoutingSignature(TypedDict):
    proposed_calls: list[_ProposedCall]


class _RoutingV4(TypedDict):
    proposed_routing_signature: _RoutingSignature


class _RouterDiagnostics(TypedDict):
    routing_v4: _RoutingV4


class _Resolution(TypedDict):
    market_id: str


class _RoutingResult(TypedDict):
    router_diagnostics: _RouterDiagnostics


class _GateResult(TypedDict):
    tool_calls: list[_MarketCall]
    router_diagnostics: _RouterDiagnostics
    resolution: _Resolution


def _market_call(market_id: str) -> _MarketCall:
    return {
        "tool": "get_brand_metric",
        "render_data": {
            "brand": "리바로",
            "market_id": market_id,
            "measure": "sales",
            "sales_억원": 80.39,
            "period": "2026-05",
            "source": "UBIST",
            "view_type": "market_landscape",
        },
    }


def _routing_result(market_id: str) -> _RoutingResult:
    return {
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "proposed_calls": [
                        {
                            "normalized_args": {
                                "market_id": market_id,
                                "brand": "리바로",
                            },
                        },
                    ],
                },
            },
        },
    }


def _sales_facts() -> tuple[EvidenceFact, ...]:
    facts = evidence_from_calls(
        [_market_call("ml_555"), _market_call("ml_566")],
        "",
    )
    return tuple(fact for fact in facts if fact.metric == "매출")


def test_same_value_in_different_market_ids_has_distinct_internal_scope() -> None:
    sales_facts = _sales_facts()

    assert len(sales_facts) == 2
    assert fact_scope(sales_facts[0]) == "market_landscape:ml_555"
    assert fact_scope(sales_facts[1]) == "market_landscape:ml_566"
    assert fact_scope(sales_facts[0]) != fact_scope(sales_facts[1])


def test_foreign_market_fact_cannot_hijack_same_value_claim() -> None:
    correct, foreign = _sales_facts()
    expected_market_ids = evidence_binding.expected_market_ids_from_result(
        _routing_result("ml_555"),
    )

    assert expected_market_ids == frozenset({"ml_555"})
    assert scope_matches(
        correct,
        frozenset({"market_landscape"}),
        expected_market_ids,
    ) is True
    assert scope_matches(
        foreign,
        frozenset({"market_landscape"}),
        expected_market_ids,
    ) is False

    result = evidence_binding.verify_claim_bindings(
        question="리바로 전략뷰 매출 알려줘",
        answer="리바로 전략뷰 매출은 80.39억원입니다.",
        facts=(foreign,),
        expected_entities=("리바로",),
        expected_market_ids=expected_market_ids,
    )

    assert result.status == "fail"
    assert "SCOPE_MISMATCH" in result.blocked_reasons
    assert "80.39억원" not in result.answer


def test_correct_market_fact_with_same_value_still_passes() -> None:
    correct, _foreign = _sales_facts()
    result = evidence_binding.verify_claim_bindings(
        question="리바로 전략뷰 매출 알려줘",
        answer="리바로 전략뷰 매출은 80.39억원입니다.",
        facts=(correct,),
        expected_entities=("리바로",),
        expected_market_ids=frozenset({"ml_555"}),
    )

    assert result.status == "pass"
    assert "80.39억원" in result.answer


def test_service_gate_uses_router_market_id_to_reject_foreign_fact() -> None:
    routing = _routing_result("ml_555")
    result: _GateResult = {
        "tool_calls": [_market_call("ml_566")],
        "router_diagnostics": routing["router_diagnostics"],
        "resolution": {"market_id": "ml_555"},
    }

    answer = _apply_evidence_binding_gate(
        "리바로 전략뷰 매출 알려줘",
        "리바로 전략뷰 매출은 80.39억원입니다.",
        result,
    )

    assert "80.39억원" not in answer
    gate = result["_qa_claim_gate"]
    assert gate["disposition"] == "unavailable"
    assert "SCOPE_MISMATCH" in gate["blocked_reasons"]


def test_resolution_market_id_takes_priority_over_routing_fallback() -> None:
    result: dict[str, object] = {
        **_routing_result("ml_566"),
        "resolution": {"market_id": "ml_555"},
    }

    assert evidence_binding.expected_market_ids_from_result(result) == frozenset(
        {"ml_555"},
    )


def test_market_id_never_renders_in_public_evidence_table() -> None:
    public_evidence = evidence_markdown(_sales_facts())

    assert "market_id" not in public_evidence
    assert "ml_555" not in public_evidence
    assert "ml_566" not in public_evidence


def test_legacy_view_suffix_remains_compatible_with_market_matching() -> None:
    legacy = EvidenceFact(
        fact_id="legacy",
        label="매출",
        value="80.39억원",
        source="UBIST",
        tool="get_brand_metric",
        path="render_data.sales_krw",
        period="2026-05",
        allowed_numbers=("80.39억원",),
        entity="리바로",
        metric="매출",
        unit="억원",
        source_grade=SourceGrade.AUTHORITATIVE.value,
        view="market_landscape:ml_555",
    )

    assert scope_matches(
        legacy,
        frozenset({"market_landscape"}),
        frozenset({"ml_555"}),
    ) is True
