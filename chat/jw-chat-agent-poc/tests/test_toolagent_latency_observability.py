from __future__ import annotations

from dataclasses import dataclass, field
import time

from pydantic import BaseModel
import requests

from jw_chat_agent_poc.common.timing import finish, new_timing
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.specs import ToolSpec


class _NoInput(BaseModel):
    pass


@dataclass(slots=True)
class _DelayedProvider:
    choices: tuple[ToolChoice, ...]
    delay_s: float = 0.002
    calls: int = field(default=0, init=False)

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        time.sleep(self.delay_s)
        choice = self.choices[self.calls]
        self.calls += 1
        return choice


def _fact() -> EvidenceFact:
    return EvidenceFact(
        fact_id="latency:fixture:1",
        subject="fixture",
        metric="status",
        value=None,
        unit=None,
        period=None,
        source_name="fixture source",
        source_locator="fixture",
        raw_ref=None,
    )


def test_executor_records_internal_planner_and_tool_elapsed_without_public_projection() -> None:
    timing = new_timing()
    provider = _DelayedProvider((ToolChoice("fixture_tool", {}, "run", call_id="call-1"),))

    def execute(_payload: _NoInput) -> ToolEnvelope:
        time.sleep(0.003)
        return ToolEnvelope(
            ok=True,
            preview="verified",
            evidence=(_fact(),),
            raw=None,
            error_code=None,
            error_message=None,
        )

    result = AgentExecutor(provider=provider, timing=timing).run(
        user_text="fixture question",
        tools=(
            ToolSpec(
                name="fixture_tool",
                description="fixture",
                input_model=_NoInput,
                execute=execute,
                timeout_s=1.0,
                tags=("fixture",),
            ),
        ),
    )

    assert result.status == "ok"
    observations = timing["_internal_latency_observations"]
    assert [(item["phase"], item["step"], item["operation"]) for item in observations] == [
        ("planner", 1, "choose"),
        ("tool_call", 1, "fixture_tool"),
    ]
    assert all(item["elapsed_ms"] > 0 for item in observations)
    assert all(item["started_at"].endswith("+00:00") for item in observations)
    assert all(item["ended_at"].endswith("+00:00") for item in observations)
    public_timing = finish(timing)
    assert "_internal_latency_observations" not in public_timing
    assert "planner" not in str(public_timing)


def test_final_generation_records_each_attempt_and_outcome(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_GENERATION_ATTEMPTS", "2")
    timing = new_timing()
    calls = 0

    def stream(_self: GenosClient, _messages: list[dict[str, str]]):
        nonlocal calls
        calls += 1
        time.sleep(0.002)
        if calls == 1:
            raise requests.Timeout("fixture timeout")
        yield "verified"

    monkeypatch.setattr(GenosClient, "_stream_chat", stream)
    client = GenosClient(token="fixture-token", timeout_s=1, total_budget_s=2)

    answer = client._chat_text([{"role": "user", "content": "fixture"}], timing=timing)

    assert answer == "verified"
    attempts = [
        item
        for item in timing["_internal_latency_observations"]
        if item["phase"] == "final_generation"
    ]
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert [item["status"] for item in attempts] == ["error", "ok"]
    assert all(item["elapsed_ms"] > 0 for item in attempts)
    assert all(item["started_at"].endswith("+00:00") for item in attempts)
    assert all(item["ended_at"].endswith("+00:00") for item in attempts)


def test_trace_projects_latency_diagnostics_without_putting_them_in_answer() -> None:
    timing = new_timing()
    timing["_internal_latency_observations"] = [
        {
            "phase": "planner",
            "step": 1,
            "operation": "choose",
            "attempt": None,
            "elapsed_ms": 12.5,
            "status": "ok",
        },
        {
            "phase": "tool_call",
            "step": 1,
            "operation": "fixture_tool",
            "attempt": None,
            "elapsed_ms": 25.0,
            "status": "ok",
        },
        {
            "phase": "final_generation",
            "step": None,
            "operation": "genos",
            "attempt": 1,
            "elapsed_ms": 37.5,
            "status": "ok",
        },
    ]
    answer = "Verified answer."
    result = {
        "context_scope": "MARKET",
        "router_diagnostics": {"mode": "tool_use_agent"},
        "timing": timing,
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "data_md": ""},
    }

    trace = trace_envelope(
        question="fixture",
        result=result,
        answer=answer,
        charts=(),
        timing=finish(timing),
        conversation_id="fixture-conversation",
    )

    latency = trace["qa_trace"]["latency"]
    assert latency["planner_steps"][0]["elapsed_ms"] == 12.5
    assert latency["tool_calls"][0]["operation"] == "fixture_tool"
    assert latency["final_generation"]["attempt_count"] == 1
    assert latency["final_generation"]["total_elapsed_ms"] == 37.5
    assert "_internal_latency_observations" not in str(finish(timing))
    assert "elapsed_ms" not in answer
