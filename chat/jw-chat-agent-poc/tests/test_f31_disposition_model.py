from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.answer_safety import (
    enforce_relational_numeric_claims_with_trace,
)
from jw_chat_agent_poc.service.app import (
    _apply_evidence_binding_gate,
    _apply_relational_claim_gate,
)
from jw_chat_agent_poc.service.evidence_binding import verify_claim_bindings
from jw_chat_agent_poc.service.failure_disposition import failure_kind as detect_failure_kind
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope


FAILURE_CASES = (
    (
        "이 질문에 맞는 도구가 없습니다.",
        "no_tool_planned",
    ),
    (
        "요청한 이름과 일치하는 브랜드가 없습니다. 브랜드명을 확인해 주세요.",
        "entity_not_found",
    ),
    (
        "HIRA 급여기준 실시간 조회 시간이 초과되었습니다.",
        "tool_timeout",
    ),
)


@pytest.mark.parametrize(("answer", "failure_kind"), FAILURE_CASES)
def test_runtime_trace_does_not_fall_through_failures_to_answered(
    answer: str,
    failure_kind: str,
) -> None:
    trace = trace_envelope(
        question="설명해줘",
        result={"tool_calls": [], "markdown_response": {"fact_md": "", "data_md": ""}},
        answer=answer,
        charts=(),
        timing={"stages": []},
        conversation_id="f31-runtime",
    )

    assert trace["qa_trace"]["final"] == {
        "disposition": "unavailable",
        "body_empty": False,
        "failure_kind": failure_kind,
    }


@pytest.mark.parametrize(("answer", "failure_kind"), FAILURE_CASES)
def test_relational_gate_does_not_fall_through_failures_to_answered(
    answer: str,
    failure_kind: str,
) -> None:
    gate = enforce_relational_numeric_claims_with_trace(
        "설명해줘",
        answer,
        (),
    )

    assert gate.disposition == "unavailable"
    assert gate.failure_kind == failure_kind


@pytest.mark.parametrize(("answer", "failure_kind"), FAILURE_CASES)
def test_binding_gate_does_not_fall_through_failures_to_answered(
    answer: str,
    failure_kind: str,
) -> None:
    gate = verify_claim_bindings(
        question="설명해줘",
        answer=answer,
        facts=(),
    )

    assert gate.status == "fail"
    assert gate.disposition == "unavailable"
    assert gate.failure_kind == failure_kind


def test_real_success_remains_answered_without_failure_kind() -> None:
    answer = "리바로 매출은 확인된 근거와 일치합니다."

    relational = enforce_relational_numeric_claims_with_trace(
        "리바로 매출 알려줘",
        answer,
        (),
    )
    binding = verify_claim_bindings(
        question="설명해줘",
        answer=answer,
        facts=(),
    )
    trace = trace_envelope(
        question="설명해줘",
        result={"tool_calls": [], "markdown_response": {"fact_md": "", "data_md": ""}},
        answer=answer,
        charts=(),
        timing={"stages": []},
        conversation_id="f31-success",
    )

    assert relational.disposition == "answered"
    assert relational.failure_kind is None
    assert binding.disposition == "answered"
    assert binding.failure_kind is None
    assert trace["qa_trace"]["final"] == {
        "disposition": "answered",
        "body_empty": False,
    }


def test_app_merges_failure_kind_without_adding_a_new_disposition_value() -> None:
    result: dict[str, object] = {"tool_calls": []}

    answer = _apply_relational_claim_gate(
        "설명해줘",
        "이 질문에 맞는 도구가 없습니다.",
        result,
    )
    answer = _apply_evidence_binding_gate("설명해줘", answer, result)

    gate_state = dict(result["_qa_claim_gate"])
    # binding_decision is observation, not verdict. Popping it keeps the
    # comparison below an exact, closed check on the verdict -- a genuinely
    # new verdict key would still fail here.
    observation = gate_state.pop("binding_decision", None)

    assert gate_state == {
        "blocked_claim_count": 0,
        "blocked_reasons": ("FAILURE_KIND_NO_TOOL_PLANNED",),
        "disposition": "unavailable",
        "failure_kind": "no_tool_planned",
        "binding_status": "fail",
        "blocked_numbers": (),
    }

    # The observation records WHICH return site produced that verdict without
    # contributing to it. This early failure-kind return never entered the
    # token loop, so its counts are null rather than zero.
    assert observation == {
        "decision_site": "failure_kind_passthrough",
        "substitution_triggered": False,
        "bind_attempted_count": None,
        "bind_succeeded_count": None,
        "blocked_reason_histogram": None,
    }


def test_structured_timeout_wins_over_generic_error_status() -> None:
    assert detect_failure_kind(
        "외부 조회에 실패했습니다.",
        (
            {
                "status": "error",
                "render_data": {"error_code": "TOOL_TIMEOUT"},
            },
        ),
    ) == "tool_timeout"


def test_successful_fallback_prevents_tool_failure_overclassification() -> None:
    assert detect_failure_kind(
        "공식 대체 근거로 확인했습니다.",
        (
            {"status": "timeout"},
            {"status": "ok", "render_data": {"evidence": [{"value": "확인"}]}},
        ),
    ) is None
