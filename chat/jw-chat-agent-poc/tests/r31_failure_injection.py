from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.service.v4 import runtime as runtime_module
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


NOTICE_MARKER = "축소된 범위로 조회했습니다"


def plan() -> PlannerOutput:
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
        needs_second_hop=False,
    )


def transport_error(text: str) -> CompletionTransportError:
    return CompletionTransportError(
        "read_timeout",
        partial=CompletionResult(
            text=text,
            finish_reason=None,
            usage={},
            elapsed_ms=17_000.0,
            serving_id="190",
            model="gemini-3-flash-preview",
        ),
    )


def planner_outcome(partial: str):
    class Client:
        serving_id = "190"

        def complete_detailed(self, *_args, **_kwargs):
            raise transport_error(partial)

    return V4Planner(Client()).plan_with_trace(
        "당뇨병 환자수 알려줘", (), budget_s=18.0
    )


def answer(outcome):
    class Planner:
        def plan_with_trace(self, *_args, **_kwargs):
            return outcome

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute_with_trace(self, *_args, **_kwargs):
            return SimpleNamespace(
                results=(), trace={"elapsed_ms": 1.0, "tools": []}
            )

    class Synthesizer:
        def synthesize_with_trace(self, *_args, **_kwargs):
            return SynthesisOutcome(
                text="조회 결과를 정리했습니다.",
                trace={"status": "ok", "elapsed_ms": 1.0, "usage": {}},
            )

    return V4Runtime(
        planner=Planner(), executor=Executor(), synthesizer=Synthesizer()
    ).answer("당뇨병 환자수 알려줘", conversation_id="r31-injection", turns=())


def check(label: str, condition: bool, detail: str) -> None:
    print(f"{label}: {'PASS' if condition else 'FAIL'} | {detail}")
    if not condition:
        raise AssertionError(label)


invalid = planner_outcome('{"resolved_question":"당뇨병')
invalid_answer = answer(invalid)
check(
    "F1 delayed planner -> fallback and public notice",
    invalid.trace["status"] == "fallback" and NOTICE_MARKER in invalid_answer.text,
    f"status={invalid.trace['status']} notice={NOTICE_MARKER in invalid_answer.text}",
)

original_notice = runtime_module._planner_degradation_notice
runtime_module._planner_degradation_notice = lambda _trace: None
try:
    silent_answer = answer(invalid)
finally:
    runtime_module._planner_degradation_notice = original_notice
check(
    "F2 notice disabled -> silent collapse returns",
    NOTICE_MARKER not in silent_answer.text,
    f"notice={NOTICE_MARKER in silent_answer.text}",
)

normal = SimpleNamespace(
    plan=plan(), trace={"status": "ok", "elapsed_ms": 10_000.0, "usage": {}}
)
normal_answer = answer(normal)
check(
    "F3 normal planner -> no false notice",
    NOTICE_MARKER not in normal_answer.text,
    f"notice={NOTICE_MARKER in normal_answer.text}",
)

recovered = planner_outcome(plan().model_dump_json())
recovered_answer = answer(recovered)
check(
    "F4 complete streamed partial -> validated recovery",
    recovered.trace["status"] == "partial_recovered"
    and recovered.trace["partial_plan_recovered"] is True
    and "일부 확장 정보" in recovered_answer.text,
    (
        f"status={recovered.trace['status']} "
        f"recovered={recovered.trace['partial_plan_recovered']}"
    ),
)
