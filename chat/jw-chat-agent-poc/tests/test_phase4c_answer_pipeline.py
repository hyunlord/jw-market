from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.service import answer_pipeline
from jw_chat_agent_poc.service import app as service_app


def test_extracted_pipeline_runs_declared_order() -> None:
    observed: list[str] = []

    def transform(name: str):
        def apply(answer: str) -> str:
            observed.append(name)
            return f"{answer}|{name}"

        return apply

    stages = tuple(
        answer_pipeline.AnswerPipelineStage(name, transform(name))
        for name in ("answer_contract", "evidence_binding", "final_scrub")
    )

    result = answer_pipeline.run_answer_pipeline("start", stages)

    assert result == "start|answer_contract|evidence_binding|final_scrub"
    assert observed == ["answer_contract", "evidence_binding", "final_scrub"]


def test_extracted_pipeline_preserves_failure_mode() -> None:
    def broken(_answer: str) -> str:
        raise RuntimeError("gate failed")

    stages = (answer_pipeline.AnswerPipelineStage("broken", broken),)

    with pytest.raises(RuntimeError, match="gate failed"):
        answer_pipeline.run_answer_pipeline("start", stages)


def test_stage_trace_is_absent_when_flag_is_off(monkeypatch) -> None:
    monkeypatch.setenv(answer_pipeline.ANSWER_STAGE_TRACE_ENV, "0")
    result = {"_qa_claim_gate": {"pipeline_observability": {"existing": True}}}
    stages = (
        answer_pipeline.AnswerPipelineStage("append", lambda answer: f"{answer}\nnew"),
    )

    wrapped = answer_pipeline.instrument_answer_pipeline_stages(
        stages,
        result=result,
        markdown_response={"evidence": [{"fact_id": "fact-1"}]},
    )
    answer_pipeline.run_answer_pipeline("start", wrapped)

    observability = result["_qa_claim_gate"]["pipeline_observability"]
    assert observability == {"existing": True}


def test_stage_trace_records_only_ids_and_preserves_existing_fields(monkeypatch) -> None:
    monkeypatch.setenv(answer_pipeline.ANSWER_STAGE_TRACE_ENV, "1")
    result = {
        "_qa_claim_gate": {
            "blocked_claim_count": 2,
            "pipeline_observability": {"existing": True},
        }
    }
    stages = (
        answer_pipeline.AnswerPipelineStage(
            "replace",
            lambda _answer: "replacement confidential sentence",
        ),
    )

    wrapped = answer_pipeline.instrument_answer_pipeline_stages(
        stages,
        result=result,
        markdown_response={"evidence": [{"fact_id": "fact-1"}, {"evidence_id": "fact-2"}]},
    )
    answer_pipeline.run_answer_pipeline("original confidential sentence", wrapped)

    gate = result["_qa_claim_gate"]
    trace = gate["pipeline_observability"]["answer_assembly_v1"]
    assert gate["blocked_claim_count"] == 2
    assert gate["pipeline_observability"]["existing"] is True
    assert trace["redaction"] == "ids_only_no_user_text"
    assert trace["stages"] == [
        {
            "seq": 1,
            "name": "replace",
            "before": {"fact_ids": ["fact-1", "fact-2"], "claim_ids": ["c0001"]},
            "after": {"fact_ids": ["fact-1", "fact-2"], "claim_ids": ["c0002"]},
            "diff": {
                "fact_ids": {"added": [], "removed": []},
                "claim_ids": {"added": ["c0002"], "removed": ["c0001"]},
            },
        }
    ]
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "original confidential sentence" not in serialized
    assert "replacement confidential sentence" not in serialized


def test_flag_off_does_not_call_extracted_runner(monkeypatch) -> None:
    monkeypatch.setenv(answer_pipeline.ANSWER_PIPELINE_ENV, "0")

    def unexpected(_answer: str, _stages: tuple[answer_pipeline.AnswerPipelineStage, ...]) -> str:
        raise AssertionError("extracted answer pipeline must remain disabled")

    monkeypatch.setattr(answer_pipeline, "run_answer_pipeline", unexpected)

    assert answer_pipeline.run_selected_answer_pipeline(
        "legacy",
        (),
        legacy=lambda answer: f"{answer}|unchanged",
    ) == "legacy|unchanged"


def test_app_pipeline_flag_on_and_off_are_byte_identical(monkeypatch) -> None:
    monkeypatch.setattr(
        service_app,
        "_deterministic_simple_market_answer",
        lambda *_args: "리바로 매출은 80.39억원입니다.",
    )
    payload = {"tool_calls": [], "sources": []}

    monkeypatch.setenv(answer_pipeline.ANSWER_PIPELINE_ENV, "0")
    legacy = service_app._compute_final_answer("리바로 매출 알려줘", dict(payload))
    monkeypatch.setenv(answer_pipeline.ANSWER_PIPELINE_ENV, "1")
    extracted = service_app._compute_final_answer("리바로 매출 알려줘", dict(payload))

    assert extracted.text.encode() == legacy.text.encode()
    assert extracted.charts == legacy.charts


def test_app_pipeline_emits_exact_declared_stage_order(monkeypatch) -> None:
    observed: list[str] = []
    original = answer_pipeline.run_answer_pipeline

    def traced(answer: str, stages: tuple[answer_pipeline.AnswerPipelineStage, ...]) -> str:
        observed.extend(stage.name for stage in stages)
        return original(answer, stages)

    monkeypatch.setenv(answer_pipeline.ANSWER_PIPELINE_ENV, "1")
    monkeypatch.setattr(answer_pipeline, "run_answer_pipeline", traced)
    monkeypatch.setattr(
        service_app,
        "_deterministic_simple_market_answer",
        lambda *_args: "리바로 매출은 80.39억원입니다.",
    )

    service_app._compute_final_answer("리바로 매출 알려줘", {"tool_calls": [], "sources": []})

    assert observed == [
        *answer_pipeline.PRE_CHART_STAGE_NAMES,
        *answer_pipeline.POST_CHART_STAGE_NAMES,
    ]


def test_app_stage_trace_covers_declared_pipeline_without_answer_text(monkeypatch) -> None:
    monkeypatch.setenv(answer_pipeline.ANSWER_PIPELINE_ENV, "1")
    monkeypatch.setenv(answer_pipeline.ANSWER_STAGE_TRACE_ENV, "1")
    monkeypatch.setattr(
        service_app,
        "_deterministic_simple_market_answer",
        lambda *_args: "confidential rendered answer",
    )
    payload = {
        "tool_calls": [],
        "sources": [],
        "evidence": [{"fact_id": "fact-app-1"}],
    }

    service_app._compute_final_answer("confidential user question", payload)

    trace = payload["_qa_claim_gate"]["pipeline_observability"]["answer_assembly_v1"]
    assert [stage["name"] for stage in trace["stages"]] == [
        *answer_pipeline.PRE_CHART_STAGE_NAMES,
        *answer_pipeline.POST_CHART_STAGE_NAMES,
    ]
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "confidential rendered answer" not in serialized
    assert "confidential user question" not in serialized


def test_app_flag_off_never_uses_extracted_runner(monkeypatch) -> None:
    monkeypatch.setenv(answer_pipeline.ANSWER_PIPELINE_ENV, "0")
    monkeypatch.setattr(
        service_app,
        "_deterministic_simple_market_answer",
        lambda *_args: "리바로 매출은 80.39억원입니다.",
    )

    def unexpected(*_args, **_kwargs) -> str:
        raise AssertionError("extracted answer pipeline must remain disabled")

    monkeypatch.setattr(answer_pipeline, "run_answer_pipeline", unexpected)

    final = service_app._compute_final_answer(
        "리바로 매출 알려줘",
        {"tool_calls": [], "sources": []},
    )

    assert "80.39억원" in final.text
