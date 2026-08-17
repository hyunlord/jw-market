from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    PlannerOutput,
    RequestedAnswerShape,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.llm import CompletionResult, CompletionTransportError
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.runtime import V4Runtime
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome


def _plan() -> PlannerOutput:
    return PlannerOutput(
        resolved_question="당뇨병 환자수 알려줘",
        expanded_intents=("환자수",),
        answer_sources=("hira",),
        tool_queries=ToolQueries(
            **{source: (f"{source} query",) for source in SOURCE_NAMES}
        ),
        linking_plan="질환 환자수 근거를 조회",
        requested_answer_shape=RequestedAnswerShape(
            measure_or_attribute=("patient_count",)
        ),
        needs_second_hop=True,
    )


def _transport_error(text: str) -> CompletionTransportError:
    return CompletionTransportError(
        "read_timeout",
        partial=CompletionResult(
            text=text,
            finish_reason=None,
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            elapsed_ms=17_000.0,
            serving_id="190",
            model="gemini-3-flash-preview",
        ),
    )


def test_first_planner_call_receives_the_complete_budget() -> None:
    observed: list[float] = []

    class Client:
        serving_id = "190"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            observed.append(budget_s)
            return CompletionResult(
                text=_plan().model_dump_json(),
                finish_reason="stop",
                usage={},
                elapsed_ms=1.0,
            )

    outcome = V4Planner(Client()).plan_with_trace(
        "당뇨병 환자수 알려줘", (), budget_s=18.0
    )

    assert outcome.trace["status"] == "ok"
    assert observed == [18.0]


def test_complete_partial_planner_contract_is_recovered() -> None:
    class Client:
        serving_id = "190"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            raise _transport_error(_plan().model_dump_json())

    outcome = V4Planner(Client()).plan_with_trace(
        "당뇨병 환자수 알려줘", (), budget_s=18.0
    )

    assert outcome.plan.resolved_question == "당뇨병 환자수 알려줘"
    assert outcome.trace["status"] == "partial_recovered"
    assert outcome.trace["degradation_reason"] == "read_timeout"
    assert outcome.trace["partial_plan_recovered"] is True


def test_incomplete_partial_falls_back_with_typed_reason() -> None:
    class Client:
        serving_id = "190"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            raise _transport_error('{"resolved_question":"당뇨병')

    outcome = V4Planner(Client()).plan_with_trace(
        "당뇨병 환자수 알려줘", (), budget_s=18.0
    )

    assert outcome.trace["status"] == "fallback"
    assert outcome.trace["degradation_reason"] == "read_timeout"
    assert outcome.trace["partial_plan_recovered"] is False


class _Executor:
    def execute_with_trace(self, _plan, **_kwargs):
        return SimpleNamespace(results=(), trace={"elapsed_ms": 1.0, "tools": []})


class _Synthesizer:
    def synthesize_with_trace(self, *_args, **_kwargs):
        return SynthesisOutcome(
            text="조회 결과를 정리했습니다.",
            trace={"status": "ok", "elapsed_ms": 1.0, "usage": {}},
        )


def _runtime_answer(planner_trace: dict[str, object]) -> object:
    class Planner:
        def plan_with_trace(self, *_args, **_kwargs):
            return SimpleNamespace(plan=_plan(), trace=planner_trace)

        def link(self, *_args, **_kwargs):
            return None

    return V4Runtime(
        planner=Planner(), executor=_Executor(), synthesizer=_Synthesizer()
    ).answer("당뇨병 환자수 알려줘", conversation_id="r31-fixture", turns=())


def test_fallback_notice_reaches_the_final_answer_and_trace(caplog) -> None:
    answer = _runtime_answer(
        {
            "status": "fallback",
            "degradation_reason": "read_timeout",
            "partial_plan_recovered": False,
            "elapsed_ms": 17_000.0,
            "usage": {},
        }
    )

    public_notice = (
        "질문 해석이 시간 내 완료되지 않아 축소된 범위로 조회했습니다. "
        "이 답변은 제한된 조회 범위를 기준으로 확인해 주세요."
    )
    assert public_notice in answer.text
    assert "17.0" not in answer.text
    assert "read timeout" not in answer.text.casefold()
    assert answer.trace["planner_degradation"]["notice_shown"] is True
    assert "planner degraded" in caplog.text


def test_normal_planner_path_has_no_degradation_notice() -> None:
    answer = _runtime_answer(
        {
            "status": "ok",
            "elapsed_ms": 10_000.0,
            "usage": {},
        }
    )

    assert "축소된 범위로 조회했습니다" not in answer.text
    assert answer.trace["planner_degradation"]["notice_shown"] is False
