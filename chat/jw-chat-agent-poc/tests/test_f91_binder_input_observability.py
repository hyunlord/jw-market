from __future__ import annotations

from dataclasses import asdict
import json

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate
from jw_chat_agent_poc.service.evidence_binding import BindingVerification
from jw_chat_agent_poc.service.evidence_binding_observability import (
    binding_context_observability,
    binding_pipeline_observability,
)
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope


def _gate(*blocked_numbers: str) -> BindingVerification:
    return BindingVerification(
        answer="filtered",
        status="fail",
        disposition="partial",
        blocked_claim_count=len(blocked_numbers),
        blocked_reasons=("MISSING_EVIDENCE_BINDING",),
        blocked_numbers=blocked_numbers,
    )


def _observability(
    answer: str,
    *blocked_numbers: str,
    context_projection_allowed: bool = True,
) -> dict[str, object]:
    gate = _gate(*blocked_numbers)
    pipeline = binding_pipeline_observability(
        question="리바로 매출 알려줘",
        answer=answer,
        facts=(),
        expected_entities=("리바로",),
        expected_market_ids=frozenset(),
        gate=gate,
        fact_input={
            "source": "reconstructed_from_tool_calls",
            "input_item_count": 0,
            "loaded_fact_count": 0,
            "discarded_count": 0,
            "discard_reason": "",
        },
    )
    context = binding_context_observability(
        question="리바로 매출 알려줘",
        answer=answer,
        expected_entities=("리바로",),
        gate=gate,
        context_projection_allowed=context_projection_allowed,
    )
    return {**pipeline, **context}


def test_binder_and_pre_binding_contexts_expose_only_bounded_metadata() -> None:
    answer = (
        "리바로 매출 설명입니다. "
        "2025-12 매출은 90.86억원이고 2026-02 매출은 75.08억원입니다."
    )

    trace = _observability(
        answer,
        "2025-12",
        "90.86억원",
        "2026-02",
        "75.08억원",
    )

    binder_context = trace["binder_input_context"]
    pre_binding_context = trace["pre_binding_answer_context"]
    assert binder_context["scope"] == "blocked_token_context_metadata"
    assert pre_binding_context["scope"] == "blocked_token_context_metadata"
    assert binder_context["available"] is True
    assert pre_binding_context["available"] is True
    assert len(binder_context["contexts"]) == 4
    assert len(pre_binding_context["contexts"]) == 4
    assert binder_context["source_text_included"] is False
    assert pre_binding_context["source_text_included"] is False
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "리바로 매출 설명입니다" not in serialized
    assert '"text"' not in serialized


def test_blocked_occurrences_are_prioritized_within_existing_eight_item_cap() -> None:
    prefix = " ".join(f"{index + 10}.11억원" for index in range(20))
    blocked = (
        "75.08억원",
        "90.86억원",
        "2025-12",
        "2026-02",
        "2025-08",
        "0.17%p",
        "0.76억원",
    )
    answer = f"{prefix}\n" + " ".join(blocked)

    trace = _observability(answer, *blocked)

    assert trace["occurrence_count"] > 8
    assert trace["occurrences_emitted"] == 8
    assert trace["occurrences_truncated"] is True
    emitted_blocked_refs = {
        item["token_ref"]
        for item in trace["occurrences"]
        if item["decision"] == "blocked"
    }
    expected_blocked_refs = {
        item["token_ref"]
        for item in trace["binder_input"]["blocked_token_refs"]
    }
    assert emitted_blocked_refs == expected_blocked_refs


def test_file_grounded_answer_omits_context_metadata() -> None:
    sensitive = "환자 홍길동의 2026-02 수치는 75.08억원입니다."

    trace = _observability(
        sensitive,
        "2026-02",
        "75.08억원",
        context_projection_allowed=False,
    )

    serialized = json.dumps(trace, ensure_ascii=False)
    assert trace["binder_input_context"] == {
        "scope": "blocked_token_context_metadata",
        "available": False,
        "omitted_reason": "file_grounded_answer",
        "source_text_included": False,
        "contexts": (),
        "context_count": 0,
        "contexts_truncated": False,
        "projected_context_chars": 0,
    }
    assert trace["pre_binding_answer_context"] == trace["binder_input_context"]
    assert "홍길동" not in serialized


def test_context_projection_is_bounded_and_marks_truncation() -> None:
    blocked = tuple(f"{index + 100}.123억원" for index in range(16))
    answer = "\n".join(
        f"{'문맥' * 80} {token} {'추가' * 80}"
        for token in blocked
    )

    trace = _observability(answer, *blocked)

    for key in ("binder_input_context", "pre_binding_answer_context"):
        projection = trace[key]
        assert projection["projected_context_chars"] <= 2_048
        assert projection["context_count"] <= 8
        assert projection["contexts_truncated"] is True
        assert all(
            item["context_chars"] <= 256 for item in projection["contexts"]
        )
        assert all("text" not in item for item in projection["contexts"])


def test_missing_blocked_token_never_exposes_unrelated_context() -> None:
    trace = _observability(
        "민감한 주변 문맥 80.39억원",
        "999.99억원",
    )

    assert trace["binder_input_context"]["available"] is False
    assert (
        trace["binder_input_context"]["omitted_reason"]
        == "blocked_token_not_found"
    )
    assert trace["binder_input_context"]["contexts"] == ()
    assert trace["pre_binding_answer_context"]["contexts"] == ()
    assert "민감한 주변 문맥" not in json.dumps(trace, ensure_ascii=False)


def test_safe_context_metadata_reaches_public_trace_and_file_answers_remain_omitted() -> None:
    fact = EvidenceFact(
        fact_id="market-size",
        label="시장규모",
        value="75.08억원",
        source="UBIST",
        tool="get_brand_metric",
        path="render_data.market_size",
        period="2025-12",
        allowed_numbers=("75.08억원",),
        entity="리바로",
        metric="시장규모",
        unit="억원",
        source_grade="AUTHORITATIVE",
        view="general_view",
        market_id="566",
    )
    result: dict[str, object] = {
        "general_view_ready": True,
        "resolution": {"market_id": "566"},
        "markdown_response": {"evidence": [asdict(fact)]},
        "tool_calls": [],
    }
    answer = "리바로 매출은 75.08억원입니다."
    final_answer = _apply_evidence_binding_gate(
        "리바로 매출 알려줘",
        answer,
        result,
    )

    trace = trace_envelope(
        question="리바로 매출 알려줘",
        result=result,
        answer=final_answer,
        charts=(),
        timing={"stages": []},
        conversation_id="f91-runtime",
    )

    claims = trace["qa_trace"]["claims"]
    assert claims["binder_input_context"]["available"] is True
    assert claims["pre_binding_answer_context"]["available"] is True
    assert claims["binder_input_context"]["contexts"]
    serialized_claims = json.dumps(claims, ensure_ascii=False)
    assert answer not in serialized_claims
    assert "리바로 매출은" not in serialized_claims
    assert all(
        "text" not in item
        for key in ("binder_input_context", "pre_binding_answer_context")
        for item in claims[key]["contexts"]
    )

    file_result = dict(result)
    file_result.pop("_qa_claim_gate", None)
    file_result["file_context"] = "업로드 문서 비공개 원문"
    _apply_evidence_binding_gate(
        "리바로 매출 알려줘",
        answer,
        file_result,
    )
    serialized = json.dumps(file_result["_qa_claim_gate"], ensure_ascii=False)
    assert "업로드 문서 비공개 원문" not in serialized
    assert (
        file_result["_qa_claim_gate"]["binder_input_context"]["omitted_reason"]
        == "file_grounded_answer"
    )

    scoped_result = dict(result)
    scoped_result.pop("_qa_claim_gate", None)
    scoped_result["context_scope"] = "MIXED"
    _apply_evidence_binding_gate(
        "리바로 매출 알려줘",
        answer,
        scoped_result,
    )
    assert (
        scoped_result["_qa_claim_gate"]["binder_input_context"]["omitted_reason"]
        == "file_grounded_answer"
    )
