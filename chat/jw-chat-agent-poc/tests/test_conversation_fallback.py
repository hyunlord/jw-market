from __future__ import annotations

from jw_chat_agent_poc.orchestrator.agent import ChatAgent, _conversation_fallback
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


def test_ambiguous_market_question_asks_what_to_analyze() -> None:
    result = _conversation_fallback("리바로 어때?")

    assert result is not None
    assert "매출 추이" in result["answer"]
    assert "경쟁 구도" in result["answer"]
    assert "임상" in result["answer"]


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
