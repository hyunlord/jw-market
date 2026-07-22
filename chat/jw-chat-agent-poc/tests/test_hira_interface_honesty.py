from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.agent import ChatAgent


DIRECT_HIRA_ABSENT_QUESTIONS = (
    "HIRA: 상병코드 D693 환자수 알려줘",
    "상병코드 E11 2024년 환자수",
    "면역혈소판감소증 환자수 알려줘",
)

SUPPORTED_BRAND_HIRA_QUESTIONS = (
    "타발리스 환자수 알려줘",
    "가드메트 관련 질병 환자 통계 알려줘",
    "리바로 환자수 알려줘",
    "헴리브라 환자수 알려줘",
    "페린젝트 환자수 알려줘",
    "악템라 환자수 알려줘",
    "트루패스 환자수 알려줘",
    "뉴트로진 환자수 알려줘",
)


@pytest.mark.parametrize("question", DIRECT_HIRA_ABSENT_QUESTIONS)
def test_direct_hira_subject_search_absence_does_not_guess_code_or_stats(question: str) -> None:
    agent = ChatAgent(external_mode="fixture")

    for _ in range(5):
        result = agent.answer(question)
        answer = str(result.get("answer") or "")

        assert result["sources"] == ["hira_disease"]
        assert result["tool_calls"][0]["tool"] == "hira_disease_code_absent"
        assert result["tool_calls"][0]["status"] == "no_data"
        assert all(
            call.get("tool") != "hira_disease_hospitalization_outpatient_stats"
            for call in result["tool_calls"]
        )
        assert "E78" not in answer
        assert "데이터가 없습니다" not in answer


@pytest.mark.parametrize("question", SUPPORTED_BRAND_HIRA_QUESTIONS)
def test_supported_brand_hira_questions_keep_their_mapped_calls(question: str) -> None:
    result = ChatAgent(external_mode="fixture").answer(question)
    tools = {str(call.get("tool") or "") for call in result.get("tool_calls", [])}

    assert result["sources"] == ["hira_disease"]
    assert "hira_disease_mapping" in tools or "get_disease_stats" in tools
    assert "unsupported_hira_interface" not in str(result.get("answer") or "")
