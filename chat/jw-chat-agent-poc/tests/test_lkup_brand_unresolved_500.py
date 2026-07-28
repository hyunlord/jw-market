"""LKUP — an unresolved brand must ask, not return 500.

The planner already says what it wants: "ask the user to specify a brand". It
just says it by raising an exception that nothing on the direct agent-loop path
catches, so uvicorn turns it into Internal Server Error and the user gets no
answer, no reason, and no qa_trace — the trace envelope is built inside
compute_final_answer, which the escaping exception preempts.

The catch is deliberately narrow. LookupError is raised in ~60 places in this
package and UnsupportedBrandError, AmbiguousBrandError and CauseBackendError are
all subclasses of it, so `except LookupError` here would swallow genuine data
failures ("mart market not found", "latest period is missing") and report them
as a brand question. A dedicated subclass keeps every one of those paths exactly
where it was.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jw_chat_agent_poc.agent_loop.planner import BrandUnresolvedError, _brand
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import create_app

# The three-way conjunction VWX isolated: a disease name (so no brand resolves),
# a view word that does not open the general-view route, and a named metric.
UNRESOLVED_QUESTIONS = (
    "고지혈증 전략뷰 HHI 알려줘",
    "고지혈증 market_landscape HHI 알려줘",
)
ALLOWED_BRANDS = ("리바로", "리바로젯", "악템라", "헴리브라")


class _RealRaiseAgent:
    """Reaches the production raise site instead of imitating it."""

    def __init__(self, question_brands: tuple[str, ...] = ALLOWED_BRANDS) -> None:
        self.question_brands = question_brands
        self.calls: list[str] = []

    def answer(self, question: str, *args: object, **kwargs: object) -> dict:
        del args, kwargs
        self.calls.append(question)
        _brand(question, self.question_brands)  # raises BrandUnresolvedError
        raise AssertionError("brand unexpectedly resolved")


class _OtherLookupErrorAgent:
    """A data-layer LookupError, of the kind that must keep escaping."""

    def answer(self, question: str, *args: object, **kwargs: object) -> dict:
        del question, args, kwargs
        raise LookupError("mart market not found: market=ml_006")


# ─── the raise site itself ──────────────────────────────────────────────────


@pytest.mark.parametrize("question", UNRESOLVED_QUESTIONS)
def test_planner_raises_brand_unresolved_for_a_disease_named_question(question: str) -> None:
    with pytest.raises(BrandUnresolvedError) as excinfo:
        _brand(question, ALLOWED_BRANDS)

    assert "ask the user to specify a brand" in str(excinfo.value)
    # Still a LookupError, so every existing `except LookupError` keeps working.
    assert isinstance(excinfo.value, LookupError)


def test_multiple_brand_match_also_raises_brand_unresolved() -> None:
    with pytest.raises(BrandUnresolvedError) as excinfo:
        _brand("리바로와 리바로젯 매출 비교해줘", ALLOWED_BRANDS)

    assert "multiple brands matched" in str(excinfo.value)


def test_resolved_brand_does_not_raise() -> None:
    assert _brand("리바로 전략뷰 매출 알려줘", ALLOWED_BRANDS) == "리바로"


# ─── the hole: direct agent loop ────────────────────────────────────────────


@pytest.mark.parametrize("question", UNRESOLVED_QUESTIONS)
def test_direct_agent_loop_returns_a_question_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
) -> None:
    agent = _RealRaiseAgent()
    monkeypatch.setattr(service_app, "build_tool_use_agent", lambda _deps: agent)

    result = service_app._answer_direct_agent_loop(question, "live")

    assert agent.calls == [question]
    assert result["brand_unresolved"] is True
    assert result["sources"] == ["brand_unresolved"]
    assert result["tool_calls"] == []
    answer = str(result["answer"])
    assert "브랜드" in answer
    assert not any(character.isdigit() for character in answer)


def test_unrelated_lookup_error_still_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A data failure must not be reported as a brand question."""

    monkeypatch.setattr(
        service_app,
        "build_tool_use_agent",
        lambda _deps: _OtherLookupErrorAgent(),
    )

    with pytest.raises(LookupError) as excinfo:
        service_app._answer_direct_agent_loop("리바로 매출 알려줘", "live")

    assert "mart market not found" in str(excinfo.value)
    assert not isinstance(excinfo.value, BrandUnresolvedError)


# ─── HTTP boundary: 200, a reason, and a qa_trace ───────────────────────────


@pytest.mark.parametrize("question", UNRESOLVED_QUESTIONS)
def test_endpoint_answers_200_with_a_qa_trace(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
) -> None:
    monkeypatch.setattr(
        service_app,
        "build_tool_use_agent",
        lambda _deps: _RealRaiseAgent(),
    )
    client = TestClient(create_app())

    response = client.post("/chat/answer", json={"question": question})

    assert response.status_code == 200
    payload = response.json()
    assert "브랜드" in payload["text"]
    # The whole point: the trace envelope survives, so the next round can see
    # what happened without reading pod logs.
    qa_trace = payload["trace"]["qa_trace"]
    assert qa_trace["routing"]["route"] is not None
    assert qa_trace["final"]["body_empty"] is False


def test_endpoint_marks_the_recovery_in_the_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_app,
        "build_tool_use_agent",
        lambda _deps: _RealRaiseAgent(),
    )
    client = TestClient(create_app())

    response = client.post("/chat/answer", json={"question": UNRESOLVED_QUESTIONS[0]})

    payload = response.json()
    diagnostics = payload["trace"]["qa_trace"]["routing"]
    assert diagnostics["gate_reason"] == "brand_unresolved"
