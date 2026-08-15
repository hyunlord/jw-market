"""R12.7c retry — the transport error type must not bypass existing recovery.

R12.7c replaced ``raise requests.Timeout(...)`` inside
``_chat_completion_with_token_cap`` with a new ``CompletionTransportError``.
That function is shared by the synthesizer *and* the planner. Only the
synthesizer learned the new type, so every planner-side transport failure
stopped matching ``except requests.RequestException`` and escaped as an
unhandled 500 — the live failure this round exists to close.
"""

from __future__ import annotations

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
from jw_chat_agent_poc.service.v4.llm import CompletionResult, CompletionTransportError
from jw_chat_agent_poc.service.v4.planner import V4Planner
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
        needs_second_hop=True,
    )


def _transport_error(kind: str = "budget_timeout") -> CompletionTransportError:
    return CompletionTransportError(
        kind,
        partial=CompletionResult(text="", finish_reason=None, usage={}, elapsed_ms=1.0),
    )


def test_transport_error_stays_within_the_requests_exception_contract() -> None:
    """Every pre-existing ``except requests.RequestException`` must still match.

    Two call sites relied on this before R12.7c (planner.plan_with_trace and
    planner.link). Keeping the contract at the type level protects call sites
    that are added later as well.
    """
    assert issubclass(CompletionTransportError, requests.RequestException)
    assert isinstance(_transport_error(), requests.RequestException)


def test_planner_falls_back_instead_of_raising_on_transport_error() -> None:
    class Client:
        serving_id = "planner"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            raise _transport_error()

    outcome = V4Planner(Client()).plan_with_trace("리바로젯 제네릭 임상현황", (), budget_s=5.0)

    assert outcome.plan is not None
    assert outcome.trace["status"] == "fallback"
    # The failure is recorded, not swallowed.
    assert outcome.trace["error_type"] == "CompletionTransportError"


def test_planner_link_returns_none_instead_of_raising_on_transport_error() -> None:
    class Client:
        serving_id = "planner"

        def complete(self, _messages, *, budget_s):
            raise _transport_error("read_timeout")

    assert V4Planner(Client()).link(_plan(), (), (), budget_s=5.0) is None


def test_synthesis_prompt_bounding_failure_keeps_the_grounded_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 3: a synthesis-family failure may cost commentary, never facts.

    ``bound_synthesis_messages`` runs before the synthesizer's own guard, so a
    failure there used to escape as a 500 and take the whole answer with it.
    """
    import jw_chat_agent_poc.service.v4.synthesizer as synthesizer_module

    def _explode(*_args, **_kwargs):
        raise ValueError("injected prompt bounding failure")

    monkeypatch.setattr(synthesizer_module, "bound_synthesis_messages", _explode)

    class Client:
        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            return CompletionResult(
                text="해설입니다.", finish_reason="stop", usage={}, elapsed_ms=10.0
            )

    result = SourceResult(
        source="clinicaltrials",
        query="ezetimibe AND pitavastatin",
        status="ok",
        payload={
            "studies": [
                {"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}}
            ]
        },
    )

    outcome = V4Synthesizer(Client()).synthesize_with_trace(
        _plan(), (result,), (), budget_s=30.0
    )

    assert outcome.text.strip()
    assert "injected prompt bounding failure" not in outcome.text
    assert "ValueError" not in outcome.text
    bound_trace = outcome.trace["prompt_bound"]
    assert bound_trace["applied"] is False
    assert bound_trace["strategy"] == "unbounded_after_error"
    assert bound_trace["error_type"] == "ValueError"
    assert bound_trace["records_discarded"] == 0


def test_runtime_synthesis_step_cannot_turn_into_a_500() -> None:
    """The synthesis step is the only LLM commentary stage; it must degrade."""
    from jw_chat_agent_poc.service.v4 import runtime as runtime_module

    outcome = runtime_module._synthesis_failure_outcome(
        RuntimeError("injected synthesis failure")
    )

    assert outcome.text.strip()
    assert "injected synthesis failure" not in outcome.text
    assert "RuntimeError" not in outcome.text
    assert outcome.trace["status"] == "fallback"
    assert outcome.trace["fallback_reason"] == "synthesis_step_failed"
    assert outcome.trace["error_type"] == "RuntimeError"
    assert outcome.trace["partial_generated"] is False


def test_transport_error_keeps_its_kind_and_partial_payload() -> None:
    exc = CompletionTransportError(
        "read_timeout",
        partial=CompletionResult(
            text="첫 문장입니다.", finish_reason=None, usage={}, elapsed_ms=5.0
        ),
    )
    assert exc.kind == "read_timeout"
    assert exc.partial.text == "첫 문장입니다."
    assert isinstance(exc, RuntimeError)
