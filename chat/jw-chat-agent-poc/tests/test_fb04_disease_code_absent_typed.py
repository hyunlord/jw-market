from __future__ import annotations

import hashlib

from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.orchestrator.hira_disease import (
    HiraDiseaseCodeUnavailable,
    resolve_hira_disease_code,
)
from jw_chat_agent_poc.orchestrator.typed_failure import (
    TypedFailureCode,
    normalize_typed_failure,
)
from jw_chat_agent_poc.service.app import compute_final_answer
from jw_chat_agent_poc.tools.external.client import ExternalApiClient, ExternalCall


class _UnavailableDiseaseCodeExternal(ExternalApiClient):
    def __init__(self) -> None:
        pass

    def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
        return ExternalCall(
            tool="hira_disease_name_code",
            source="hira_disease",
            status="error",
            summary_text="HIRA 질병코드 조회 실패",
            render_data={"error_code": "UPSTREAM_UNAVAILABLE", "items": []},
        )


class _CodePresentZeroStatisticsExternal(ExternalApiClient):
    def __init__(self) -> None:
        pass

    def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
        return ExternalCall(
            tool="hira_disease_name_code",
            source="hira_disease",
            status="ok",
            summary_text="HIRA 상병코드 확인",
            render_data={
                "totalCount": "1",
                "items": [{"sickCd": "D69.3", "sickNm": "면역성 혈소판감소증"}],
            },
        )

    def hira_disease_hospitalization_outpatient_stats(
        self,
        sick_cd: str,
        year: str = "2024",
    ) -> ExternalCall:
        return ExternalCall(
            tool="hira_disease_hospitalization_outpatient_stats",
            source="hira_disease",
            status="no_data",
            summary_text="HIRA 환자수 통계 0건",
            render_data={
                "totalCount": "0",
                "items": [],
                "request": {"sickCd": sick_cd, "year": year},
            },
        )


def test_fb04_absent_code_produces_canonical_actionable_terminal() -> None:
    question = "당뇨망창병증 환자수 알려줘"
    result = ChatAgent(external_mode="fixture").answer(question)

    normalized = normalize_typed_failure(result)
    final = compute_final_answer(question, result, "fb04-typed")

    assert normalized is not None
    assert normalized.code is TypedFailureCode.DISEASE_CODE_ABSENT
    assert normalized.terminal is True
    assert normalized.partial is False
    assert result["tool_calls"][0]["render_data"]["reason_code"] == "DISEASE_CODE_ABSENT"
    assert "해당 질병명에 대응하는 HIRA 상병코드를 찾지 못했습니다" in final.text
    assert "https://opendata.hira.or.kr/" in final.text
    assert "검색어: 당뇨망창병증" in final.text
    assert "확인 필드: 상병코드(KCD), 질병명" in final.text
    assert "상병코드를 직접" in final.text
    assert "요청 대상과 조회 근거의 대상·지표·기간 정합" not in final.text


def test_upstream_failure_is_not_reclassified_as_disease_code_absent() -> None:
    external = _UnavailableDiseaseCodeExternal()
    resolution = resolve_hira_disease_code("당뇨망막병증", external)
    result = ChatAgent(external=external).answer("당뇨망막병증 환자수 알려줘")

    assert isinstance(resolution, HiraDiseaseCodeUnavailable)
    assert result["tool_calls"][0]["status"] == "error"
    assert result["tool_calls"][0]["render_data"]["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert "DISEASE_CODE_ABSENT" not in str(result)


def test_code_present_but_zero_statistics_answer_is_byte_unchanged() -> None:
    question = "상병코드 D693 환자수 알려줘"
    result = ChatAgent(external=_CodePresentZeroStatisticsExternal()).answer(question)
    final = compute_final_answer(question, result, "code-present-zero-statistics")

    assert [(call["tool"], call["status"]) for call in result["tool_calls"]] == [
        ("hira_disease_name_code", "ok"),
        ("hira_disease_hospitalization_outpatient_stats", "no_data"),
    ]
    assert hashlib.sha256(final.text.encode()).hexdigest() == (
        "f4dda7910404f93f8cd9a114a010d510cb9ea2ca297af8b0560598787d494fe2"
    )
    assert "DISEASE_CODE_ABSENT" not in str(result)
