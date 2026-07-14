from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from pydantic import BaseModel

from jw_chat_agent_poc.service.answer_safety import FAIL_CLOSED_TEXT
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, FallbackCode, ToolEnvelope
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.specs import EmptyInput, ToolSpec


@dataclass(slots=True)
class _ChoiceSequence:
    choices: Sequence[ToolChoice]
    calls: int = field(default=0, init=False)

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        choice = self.choices[self.calls]
        self.calls += 1
        return choice


def _fact() -> EvidenceFact:
    return EvidenceFact(
        fact_id="local_molecule:리바로:1",
        subject="리바로",
        metric="성분",
        value=None,
        unit=None,
        period=None,
        source_name="로컬 시장 DB 성분 정보",
        source_locator="pitavastatin",
        raw_ref=None,
    )


def test_agent_executor_rejects_empty_evidence_without_retrying_generation() -> None:
    # Given: the selected tool returns no verifiable evidence.
    provider = _ChoiceSequence((ToolChoice("empty_tool", {}, "call empty tool", call_id="call-1"),))
    spec = ToolSpec(
        name="empty_tool",
        description="when to use: empty fixture. when NOT to use: unrelated questions.",
        input_model=EmptyInput,
        execute=lambda _payload: ToolEnvelope(
            ok=False,
            preview="no evidence",
            evidence=(),
            raw={"items": []},
            error_code="NO_EVIDENCE",
            error_message="검증 가능한 근거가 없습니다.",
        ),
        timeout_s=1.0,
        tags=("external",),
    )

    # When: the tool-use loop receives the empty envelope.
    result = AgentExecutor(provider=provider).run(user_text="빈 결과", tools=(spec,))

    # Then: it fails closed immediately instead of spending a final LLM budget.
    assert result.status == "fallback"
    assert result.fallback_code is FallbackCode.VERIFICATION_FAIL
    assert provider.calls == 1


def test_agent_executor_does_not_publish_raw_provider_preview() -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice("evidence_tool", {}, "call tool", call_id="call-1"),
            ToolChoice(None, {}, "planner saw resultCode=00 totalCount=21", call_id=None),
        )
    )
    spec = ToolSpec(
        name="evidence_tool",
        description="when to use: preview fixture. when NOT to use: unrelated questions.",
        input_model=EmptyInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="MCP returned resultCode=00 totalCount=21",
            evidence=(_fact(),),
            raw={"resultCode": "00", "totalCount": 21},
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("external",),
    )

    result = AgentExecutor(provider=provider).run(user_text="preview probe", tools=(spec,))

    public_payload = str((result.tool_calls, result.traces))
    assert "resultCode" not in public_payload
    assert "totalCount" not in public_payload


def test_agent_executor_classifies_unsupported_query_without_default_tool() -> None:
    # Given: the planner finds no matching ToolSpec.
    provider = _ChoiceSequence((ToolChoice(None, {}, "no matching tool", call_id=None),))

    # When: no evidence exists and no tool is selected.
    result = AgentExecutor(provider=provider).run(user_text="범위 밖 질문", tools=())

    # Then: the path ends explicitly instead of defaulting to an unrelated MFDS tool.
    assert result.fallback_code is FallbackCode.UNSUPPORTED_QUERY
    assert result.answer == "이 질문에 맞는 도구가 없습니다."


def test_agent_executor_classifies_invalid_arguments() -> None:
    # Given: the planner violates a strict input schema.
    provider = _ChoiceSequence((ToolChoice("strict_tool", {"unexpected": "value"}, "bad args", call_id="call-1"),))
    spec = ToolSpec(
        name="strict_tool",
        description="when to use: strict fixture. when NOT to use: unrelated questions.",
        input_model=EmptyInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="unused",
            evidence=(_fact(),),
            raw=None,
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("local",),
    )

    # When: arguments are validated before execution.
    result = AgentExecutor(provider=provider).run(user_text="schema probe", tools=(spec,))

    # Then: schema failure is classified and the tool is never treated as successful.
    assert result.fallback_code is FallbackCode.SCHEMA_INVALID
    assert result.tool_calls == ()


def test_agent_executor_classifies_tool_timeout() -> None:
    # Given: a tool exceeds its own declared budget.
    provider = _ChoiceSequence((ToolChoice("slow_tool", {}, "slow", call_id="call-1"),))
    release_tool = Event()

    def slow_tool(_payload: BaseModel) -> ToolEnvelope:
        release_tool.wait(timeout=1)
        return ToolEnvelope(
            ok=True,
            preview="late",
            evidence=(_fact(),),
            raw=None,
            error_code=None,
            error_message=None,
        )

    spec = ToolSpec(
        name="slow_tool",
        description="when to use: timeout fixture. when NOT to use: normal questions.",
        input_model=EmptyInput,
        execute=slow_tool,
        timeout_s=0.001,
        tags=("external",),
    )

    # When: the executor enforces the ToolSpec budget.
    result = AgentExecutor(provider=provider).run(user_text="timeout probe", tools=(spec,))
    release_tool.set()

    # Then: timeout is explicit and no empty-evidence generation attempt follows.
    assert result.fallback_code is FallbackCode.TOOL_TIMEOUT
    assert provider.calls == 1


def test_agent_executor_classifies_step_limit_even_with_partial_ledger() -> None:
    # Given: the planner keeps requesting tools and never emits a stop decision.
    provider = _ChoiceSequence((ToolChoice("evidence_tool", {}, "continue", call_id="call-1"),))
    spec = ToolSpec(
        name="evidence_tool",
        description="when to use: step fixture. when NOT to use: unrelated questions.",
        input_model=EmptyInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="verified",
            evidence=(_fact(),),
            raw=None,
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("local",),
    )

    # When: the configured single step is exhausted.
    result = AgentExecutor(provider=provider, max_steps=1).run(user_text="step probe", tools=(spec,))

    # Then: the incomplete planning loop is not silently normalized to success.
    assert result.fallback_code is FallbackCode.STEP_LIMIT


def test_tool_use_agent_answer_reuses_fact_number_safety(monkeypatch) -> None:
    # Given: the shared fact-number gate rejects a deterministic tool answer.
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.genos_client.answer_has_only_fact_numbers",
        lambda _answer, _numbers: False,
    )
    agent_result = {
        "answer": "- 리바로: 점유율 = 29.52% [로컬 시장 DB]",
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [],
        "markdown_response": {
            "fact_md": "- 리바로: 점유율 = 29.52% [로컬 시장 DB]",
            "data_md": "",
        },
    }

    # When: the service streams the completed evidence answer.
    answer = "".join(
        GenosClient(token="dummy-token").stream_answer("리바로 점유율", agent_result)
    )

    # Then: the shared safety gate fails closed instead of relaying rejected numbers.
    assert answer == FAIL_CLOSED_TEXT


def test_chat_source_has_no_unverified_retry_promises() -> None:
    # Given: user-facing chat source must not promise that retrying will fix an unknown failure.
    source_root = Path(__file__).resolve().parents[1] / "jw_chat_agent_poc"
    forbidden = ("일시적", "잠시 후", "다시 시도")

    # When: every tracked Python source is checked.
    matches = tuple(
        f"{path.relative_to(source_root)}:{phrase}"
        for path in source_root.rglob("*.py")
        for phrase in forbidden
        if phrase in path.read_text(encoding="utf-8")
    )

    # Then: no unverified retry wording remains in product source.
    assert matches == ()
