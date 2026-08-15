from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.llm import (
    CompletionTransportError,
    _chat_completion_with_token_cap,
)
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    SynthesisPolicy,
    bound_synthesis_messages,
    limit_evidence_sets_for_render,
    prune_unsupported_source_queries,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer


def _plan(**queries: tuple[str, ...]) -> PlannerOutput:
    values = {name: (f"{name} query",) for name in SOURCE_NAMES}
    values.update(queries)
    return PlannerOutput(
        resolved_question="리바로젯 제네릭 임상현황",
        expanded_intents=("임상",),
        answer_sources=("clinicaltrials",),
        tool_queries=ToolQueries(**values),
        linking_plan="clinical evidence",
        requested_answer_shape=RequestedAnswerShape(
            measure_or_attribute=("clinical_trials",)
        ),
    )


def test_budget_allocation_skips_only_below_measured_floor() -> None:
    policy = SynthesisPolicy(
        total_request_budget_s=150.0,
        max_synthesis_budget_s=75.0,
        min_synthesis_budget_s=15.0,
        prompt_char_limit=100_000,
        source_render_limit=40,
    )

    assert policy.allocate_synthesis_budget(remaining_s=80.0) == 75.0
    assert policy.allocate_synthesis_budget(remaining_s=25.0) == 25.0
    assert policy.allocate_synthesis_budget(remaining_s=14.9) is None


def test_stream_failure_preserves_completed_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def iter_lines(self, *, decode_unicode: bool):
            assert decode_unicode
            yield 'data: {"choices":[{"delta":{"content":"첫 문장입니다. "}}]}'
            yield 'data: {"choices":[{"delta":{"content":"둘째 문"}}]}'
            raise requests.ReadTimeout("injected")

        def close(self) -> None:
            return None

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())
    client = SimpleNamespace(
        base_url="https://example.invalid",
        token=None,
        model="test",
        timeout_s=30,
        total_budget_s=30,
        _extract_delta_from_data=lambda data: data["choices"][0]["delta"].get("content", ""),
    )

    with pytest.raises(CompletionTransportError) as captured:
        _chat_completion_with_token_cap(client, [{"role": "user", "content": "q"}], max_tokens=32)

    assert captured.value.kind == "read_timeout"
    assert captured.value.partial.text == "첫 문장입니다. 둘째 문"


def test_partial_synthesis_is_cut_at_sentence_boundary_and_keeps_public_notice() -> None:
    class Client:
        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            from jw_chat_agent_poc.service.v4.llm import CompletionResult

            partial = CompletionResult(
                text="확인된 시험은 23건입니다. 미완성 구절",
                finish_reason=None,
                usage={},
                elapsed_ms=12_000,
            )
            raise CompletionTransportError("read_timeout", partial=partial)

    result = SourceResult(
        source="clinicaltrials",
        query="ezetimibe AND pitavastatin",
        status="ok",
        payload={"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}}]},
    )
    outcome = V4Synthesizer(Client()).synthesize_with_trace(
        _plan(), (result,), (), budget_s=30.0
    )

    assert "확인된 시험은 23건입니다." in outcome.text
    assert "미완성 구절" not in outcome.text
    assert "일부만 표시합니다" in outcome.text
    assert "CompletionTransportError" not in outcome.text
    assert outcome.trace["status"] == "partial"
    assert outcome.trace["partial_generated"] is True


def test_prompt_bound_compacts_payload_without_mutating_results() -> None:
    records = [{"id": f"R{i:03d}", "long_text": "x" * 2_000} for i in range(80)]
    original = json.dumps(records, sort_keys=True)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": json.dumps({"external_evidence": [{"detail": {"records": records}}]})},
    ]

    bounded, trace = bound_synthesis_messages(messages, char_limit=12_000)

    assert sum(len(item["content"]) for item in bounded) <= 12_000
    json.loads(bounded[-1]["content"])
    assert trace["applied"] is True
    assert trace["records_discarded"] == 0
    assert json.dumps(records, sort_keys=True) == original
    assert trace["inspection_retains_full_payload"] is True


def test_pruning_uses_structured_source_capabilities_and_records_omissions() -> None:
    plan = _plan(
        hira=("리바로젯 처방 조제액 추이",),
        mart=("리바로젯 처방 조제액 추이",),
    ).model_copy(
        update={
            "answer_sources": ("mart",),
            "requested_answer_shape": RequestedAnswerShape(
                measure_or_attribute=("sales",)
            ),
        }
    )

    pruned, trace = prune_unsupported_source_queries(plan)

    assert pruned.tool_queries.hira == ()
    assert pruned.tool_queries.mart == ("리바로젯 처방 조제액 추이",)
    assert trace["omitted"]["hira"][0]["reason"] == "unsupported_measure"
    assert pruned.query_scope is not None
    assert "리바로젯 처방 조제액 추이" in pruned.query_scope.omitted_queries["hira"]


def test_pruning_can_be_disabled_for_bidirectional_failure_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", "0")
    plan = _plan(hira=("리바로젯 처방 조제액 추이",)).model_copy(
        update={
            "answer_sources": ("mart",),
            "requested_answer_shape": RequestedAnswerShape(
                measure_or_attribute=("sales",)
            ),
        }
    )

    unchanged, trace = prune_unsupported_source_queries(plan)

    assert unchanged.tool_queries.hira == ("리바로젯 처방 조제액 추이",)
    assert trace == {"applied": False, "disabled": True, "omitted": {}}


def test_render_cap_limits_only_surface_projection() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"clinical:{index:03d}",
            source="clinicaltrials",
            result_kind="clinical",
            payload={"nct_id": f"NCT{index:08d}"},
        )
        for index in range(55)
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-15T00:00:00+09:00",
        coverage=CoverageLedger(records_received=55, records_unique=55),
        records=records,
    )

    limited, trace = limit_evidence_sets_for_render((evidence,), per_source_limit=40)

    assert len(limited[0].records) == 40
    assert len(evidence.records) == 55
    assert trace["sources"]["clinicaltrials"] == {"shown": 40, "total": 55}
    assert "surface_render_limit" in limited[0].coverage.partial_reasons
