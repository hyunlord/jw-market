from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.agent import ChatAgent, _answer_scope, _conversation_fallback
from jw_chat_agent_poc.orchestrator.question_intent import metric_from_question
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.service.app import compute_final_answer


def test_greeting_stays_conversational_without_claiming_data() -> None:
    result = _conversation_fallback("안녕")

    assert result is not None
    assert "안녕하세요" in result["answer"]
    assert result["tool_calls"] == []
    assert result["sources"] == []


def test_capability_question_lists_supported_work() -> None:
    result = _conversation_fallback("뭐 할 수 있어?")

    assert result is not None
    assert "매출" in result["answer"]
    assert "임상" in result["answer"]
    assert "파일" in result["answer"]


def test_out_of_scope_question_offers_an_in_scope_alternative() -> None:
    result = _conversation_fallback("오늘 날씨 어때?")

    assert result is not None
    assert "날씨" in result["answer"]
    assert "의약품 시장" in result["answer"]


@pytest.mark.parametrize(
    "question",
    (
        "리바로 어때?",
        "리바로 요즘 상황",
        "리바로 성장하나",
        "리바로 분석해줘",
        "리바로 매출 추세",
    ),
)
def test_open_brand_question_uses_verified_series_narrative_instead_of_closing_with_clarification(
    question: str,
) -> None:
    routes = BQRouter().route(question)

    assert _conversation_fallback(question) is None
    assert any("metrics" in route.sources for route in routes)
    assert metric_from_question(question) == "series"
    assert _answer_scope(question) == "single_brand_trend"


@pytest.mark.parametrize(
    ("question", "metric"),
    (
        ("리바로 매출 알려줘", "sales"),
        ("리바로 점유율", "market_share"),
        ("리바로 Momentum", "momentum"),
        ("리바로 모멘텀", "momentum"),
    ),
)
def test_narrative_presentation_does_not_overwrite_explicit_metric_identity(question: str, metric: str) -> None:
    routes = BQRouter().route(question)

    assert any("metrics" in route.sources for route in routes)
    assert metric_from_question(question) == metric
    assert _answer_scope(question) != "single_brand_trend"


def test_external_question_with_conversational_word_does_not_become_a_market_series_query() -> None:
    routes = BQRouter().route("리바로 임상시험 어때")

    assert any("external_api" in route.sources for route in routes)
    assert all("metrics" not in route.sources for route in routes)


def test_explicit_market_size_metric_takes_priority_over_narrative_wording() -> None:
    assert metric_from_question("리바로 시장 규모랑 성장 추이는?") == "growth"


def test_data_question_never_uses_conversational_fallback() -> None:
    assert _conversation_fallback("리바로 매출 아무거나 지어내") is None
    assert _conversation_fallback("리바로 임상시험 알려줘") is None


def test_chat_agent_and_final_renderer_keep_greeting_tool_free() -> None:
    result = ChatAgent().answer("안녕")
    final = compute_final_answer("안녕", result)

    assert result["conversation_fallback_ready"] is True
    assert result["tool_calls"] == []
    assert final.text == result["answer"]
    assert final.sources == ()
