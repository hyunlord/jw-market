from __future__ import annotations

from dataclasses import asdict
import json

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate
from jw_chat_agent_poc.service.evidence_binding import BindingVerification
from jw_chat_agent_poc.service.evidence_binding_observability import (
    binding_pipeline_observability,
    binding_text_observability,
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
    text_projection_allowed: bool = True,
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
    text = binding_text_observability(
        question="리바로 매출 알려줘",
        answer=answer,
        expected_entities=("리바로",),
        gate=gate,
        text_projection_allowed=text_projection_allowed,
    )
    return {**pipeline, **text}


def test_binder_and_pre_binding_text_are_exposed_as_bounded_blocked_contexts() -> None:
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

    binder_text = trace["binder_input_text"]
    pre_binding_text = trace["pre_binding_answer_text"]
    assert binder_text["scope"] == "blocked_token_contexts"
    assert pre_binding_text["scope"] == "blocked_token_contexts"
    assert binder_text["available"] is True
    assert pre_binding_text["available"] is True
    assert len(binder_text["fragments"]) == 4
    assert len(pre_binding_text["fragments"]) == 4
    assert all(item["text"] for item in binder_text["fragments"])
    assert all(item["text"] for item in pre_binding_text["fragments"])
    assert binder_text["source_text_included_in_full"] is False
    assert pre_binding_text["source_text_included_in_full"] is False


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


def test_file_grounded_answer_omits_text_fragments() -> None:
    sensitive = "환자 홍길동의 2026-02 수치는 75.08억원입니다."

    trace = _observability(
        sensitive,
        "2026-02",
        "75.08억원",
        text_projection_allowed=False,
    )

    serialized = json.dumps(trace, ensure_ascii=False)
    assert trace["binder_input_text"] == {
        "scope": "blocked_token_contexts",
        "available": False,
        "omitted_reason": "file_grounded_answer",
        "source_text_included_in_full": False,
        "fragments": (),
        "fragment_count": 0,
        "fragments_truncated": False,
        "emitted_chars": 0,
    }
    assert trace["pre_binding_answer_text"] == trace["binder_input_text"]
    assert "홍길동" not in serialized


def test_text_projection_is_bounded_and_marks_truncation() -> None:
    blocked = tuple(f"{index + 100}.123억원" for index in range(16))
    answer = "\n".join(
        f"{'문맥' * 80} {token} {'추가' * 80}"
        for token in blocked
    )

    trace = _observability(answer, *blocked)

    for key in ("binder_input_text", "pre_binding_answer_text"):
        projection = trace[key]
        assert projection["emitted_chars"] <= 2_048
        assert projection["fragment_count"] <= 8
        assert projection["fragments_truncated"] is True
        assert all(len(item["text"]) <= 256 for item in projection["fragments"])


def test_missing_blocked_token_never_exposes_unrelated_context() -> None:
    trace = _observability(
        "민감한 주변 문맥 80.39억원",
        "999.99억원",
    )

    assert trace["binder_input_text"]["available"] is False
    assert (
        trace["binder_input_text"]["omitted_reason"]
        == "blocked_token_not_found"
    )
    assert trace["binder_input_text"]["fragments"] == ()
    assert trace["pre_binding_answer_text"]["fragments"] == ()
    assert "민감한 주변 문맥" not in json.dumps(trace, ensure_ascii=False)


def test_bounded_text_reaches_public_trace_and_file_answers_remain_omitted() -> None:
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
    assert claims["binder_input_text"]["available"] is True
    assert claims["pre_binding_answer_text"]["available"] is True
    assert claims["binder_input_text"]["fragments"]

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
        file_result["_qa_claim_gate"]["binder_input_text"]["omitted_reason"]
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
        scoped_result["_qa_claim_gate"]["binder_input_text"]["omitted_reason"]
        == "file_grounded_answer"
    )
