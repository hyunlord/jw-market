from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.bq_slots import contract_id_for_slots, extract_bq_slots
from jw_chat_agent_poc.agent_loop.element_ledger import (
    build_element_ledger,
    disposition_from_ledger,
)
from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope


C1_MIXED = "리바로 최근 매출/처방 추이 어때?"
C1_SALES_ONLY = "리바로 최근 매출 추이 어때?"
C1_RX_ONLY = "리바로 최근 처방 추이 어때?"
A1_MIXED = "리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?"
A1_CURRENT_ONLY = "리바로 시장 규모 얼마야?"
A1_TREND_ONLY = "리바로 시장 규모 어떻게 변해왔어?"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "false")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")


def _answer(question: str) -> dict:
    return ChatAgent(external_mode="fixture").answer(question)


def _disposition(question: str, result: dict) -> str:
    envelope = trace_envelope(
        question=question,
        result=result,
        answer=result.get("answer") or "",
        charts=[],
        timing={},
        conversation_id=None,
    )
    return str(envelope["qa_trace"]["final"]["disposition"])


# ----- U-1: a typed stop ends its own element, not the whole request ---------


def test_mixed_request_still_runs_the_supported_element() -> None:
    result = _answer(C1_MIXED)
    assert result["tool_calls"], "the sales element must still reach a tool"


def test_mixed_request_keeps_the_prescription_notice_verbatim() -> None:
    result = _answer(C1_MIXED)
    assert "현재 채팅 조회 계약에 미노출되어 확인할 수 없습니다" in (result["answer"] or "")
    assert "매출 지표로 대체하지 않습니다" in (result["answer"] or "")


def test_mixed_request_reports_partial() -> None:
    result = _answer(C1_MIXED)
    assert _disposition(C1_MIXED, result) == "partial"


def test_sales_only_request_is_unchanged() -> None:
    result = _answer(C1_SALES_ONLY)
    assert len(result["tool_calls"]) == 1
    assert result.get("status") is None


def test_prescription_only_request_is_unchanged() -> None:
    """The typed stop is the whole request here, so the old behaviour stands."""
    result = _answer(C1_RX_ONLY)
    assert result["tool_calls"] == []
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "FIELD_NOT_EXPOSED"


# ----- U-2: trend routing comes from the slots, not from a keyword list ------


def test_slots_already_recognise_the_trend_element() -> None:
    """The premise of U-2: no regex needs a new word."""
    for question in (A1_MIXED, A1_TREND_ONLY):
        slots = extract_bq_slots(question, brand="리바로", period="2026-05")
        assert "trend" in slots.modifiers
        assert contract_id_for_slots(slots) == "A1"


def test_market_scope_shortcut_defers_when_the_slots_carry_a_trend() -> None:
    from jw_chat_agent_poc.agent_loop.element_ledger import market_scope_defers_to_contract

    assert market_scope_defers_to_contract(A1_MIXED) is True
    assert market_scope_defers_to_contract(A1_TREND_ONLY) is True


@pytest.mark.parametrize(
    "question",
    [A1_CURRENT_ONLY, "아일리아 시장 HHI", "고지혈증 시장 HHI", "리바로 매출 알려줘"],
)
def test_market_scope_shortcut_is_untouched_without_a_trend(question: str) -> None:
    from jw_chat_agent_poc.agent_loop.element_ledger import market_scope_defers_to_contract

    assert market_scope_defers_to_contract(question) is False


# ----- U-3: disposition is aggregated per element ---------------------------


def test_ledger_aggregates_all_satisfied_to_answered() -> None:
    ledger = build_element_ledger(
        C1_SALES_ONLY, satisfied=("sales",), unsupported=(), failed=()
    )
    assert disposition_from_ledger(ledger) == "answered"


def test_ledger_aggregates_a_mix_to_partial() -> None:
    ledger = build_element_ledger(
        C1_MIXED, satisfied=("sales",), unsupported=("prescription",), failed=()
    )
    assert disposition_from_ledger(ledger) == "partial"


def test_ledger_aggregates_all_unsupported_to_unavailable() -> None:
    ledger = build_element_ledger(
        C1_RX_ONLY, satisfied=(), unsupported=("prescription",), failed=()
    )
    assert disposition_from_ledger(ledger) == "unavailable"


def test_ledger_aggregates_failure_to_unavailable() -> None:
    ledger = build_element_ledger(C1_SALES_ONLY, satisfied=(), unsupported=(), failed=("sales",))
    assert disposition_from_ledger(ledger) == "unavailable"


def test_empty_ledger_yields_no_opinion() -> None:
    """No elements means the ledger must not vote; the caller keeps its fallback."""
    assert disposition_from_ledger(()) is None


def test_prescription_only_request_is_not_answered() -> None:
    """★the U-3 defect: 0 tools and 0 facts used to be reported as answered."""
    result = _answer(C1_RX_ONLY)
    assert _disposition(C1_RX_ONLY, result) == "unavailable"


def test_a_non_empty_answer_alone_does_not_make_it_answered() -> None:
    result = _answer(C1_RX_ONLY)
    assert (result["answer"] or "").strip(), "the notice body is non-empty"
    assert _disposition(C1_RX_ONLY, result) != "answered"


def test_claim_gate_disposition_still_wins_over_the_ledger() -> None:
    """RC1 relies on the binding gate's own partial verdict; the ledger must not
    override it."""
    result = _answer(C1_SALES_ONLY)
    result["_qa_claim_gate"] = {"disposition": "partial", "blocked_claim_count": 7}
    assert _disposition(C1_SALES_ONLY, result) == "partial"


# ----- U-2 at the routing seam: which handler actually gets the request -----


class _RecordingResolver:
    """Stands in for MarketScopeResolver, recording the single-period shortcut."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def has_explicit_brand_anchor(self, question: str) -> bool:
        return True

    def has_explicit_named_market(self, question: str) -> bool:
        return False

    def answer(self, question: str, *, view_type: str) -> dict:
        self.calls.append(f"market_scope:{view_type}")
        return {"handler": "market_scope"}

    def answer_named_market(self, question: str) -> dict:  # pragma: no cover - guard
        self.calls.append("named_market")
        return {"handler": "named_market"}


class _RecordingAgent:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def answer(self, question: str, documents=None) -> dict:
        self._sink.append("agent_loop")
        return {"handler": "agent_loop"}


def _route_question(question: str) -> str:
    from jw_chat_agent_poc.service.app import SessionStore, _answer_existing_without_pending

    resolver = _RecordingResolver()
    sink: list[str] = []
    result = _answer_existing_without_pending(
        resolver,
        lambda **_: _RecordingAgent(sink),
        "conv-u",
        question,
        "fixture",
        None,
        SessionStore(),
    )
    return str(result.get("handler") or "?")


@pytest.mark.parametrize("question", [A1_MIXED, A1_TREND_ONLY])
def test_trend_questions_reach_the_agent_loop_instead_of_the_shortcut(question: str) -> None:
    assert _route_question(question) == "agent_loop"


def test_single_period_market_question_still_takes_the_shortcut() -> None:
    assert _route_question(A1_CURRENT_ONLY) == "market_scope"
