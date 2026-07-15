from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import requests
from fastapi.testclient import TestClient

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import FinalAnswer, _sse_events_from_final_answer, create_app
from jw_chat_agent_poc.service.file_search_client import UploadedFileSearchResult, search_uploaded_files
from jw_chat_agent_poc.service.genos_client import (
    GenosClient,
    _sanitize_preserving_analysis,
    _warn_dropped_file_tokens,
)
from jw_chat_agent_poc.service.answer_safety import (
    answer_has_only_fact_numbers,
    ensure_multi_file_evidence_coverage,
    ensure_file_absence_statement,
    fact_token_allowed,
    strict_allowed_numbers,
    uploaded_file_fact_tokens,
)


# 직전 e2e audit(fileqa-20260710-130748)에서 235 /search가 실제 반환한 DOCX 컨텍스트 요지
DOCX_FILE_CONTEXT = (
    "[1] qa_e2e_operations_brief.docx (document_id=112706)\n"
    "Synthetic Operations Brief\n\n"
    "Recommendation\n\n"
    "Project Dawn Beacon should use a phased rollout. The controlled pilot reduced "
    "processing time by exactly 37.8 percent while preserving the review checklist.\n\n"
    "The approval code is NAR-7712, and the unique evidence token is QA_E2E_DOCX_20260710_NAR7712.\n"
)

MULTI_FILE_CONTEXT = (
    "[1] pdrn_survey.xlsx\n"
    "성별 변수 SQ1과 연령 변수 SQ2가 있다.\n\n"
    "[2] dyslipidemia_di.xlsx\n"
    "CVOT와 LDL-C 강하 효과 항목이 있다.\n"
)

FAITHFUL_ANSWER = (
    "Project Dawn Beacon의 처리시간 감소율은 37.8%이며, 승인코드는 NAR-7712입니다. "
    "자료의 고유 토큰은 QA_E2E_DOCX_20260710_NAR7712입니다.\n\n"
    "**시사점 및 한계**\n이러한 효율성 개선 결과에 따라 단계적 롤아웃 방식이 권고되고 있습니다."
)


def _merged_strict(file_context: str) -> tuple[str, ...]:
    base = strict_allowed_numbers(file_context, ())
    return tuple(sorted({*base, *uploaded_file_fact_tokens(file_context)}))


def test_uploaded_file_fact_tokens_allow_requested_values() -> None:
    allowed = set(_merged_strict(DOCX_FILE_CONTEXT))
    # 충실 답변에서 추출되는 모든 숫자/코드 토큰이 허용되어야 한다 (37.8%, NAR7712, 고유 토큰 조각 포함)
    assert answer_has_only_fact_numbers(FAITHFUL_ANSWER, tuple(sorted(allowed)))
    # 종전에는 파일 유래 토큰이 미허용이었다 — 회귀 방지 대조
    assert not answer_has_only_fact_numbers(FAITHFUL_ANSWER, strict_allowed_numbers(DOCX_FILE_CONTEXT, ()))


def test_sanitize_preserves_file_grounded_answer() -> None:
    # 종전(파일 토큰 미합산) 동작: 요구 값 문장이 제거됨 — 회귀 방지용 대조
    legacy = _sanitize_preserving_analysis(FAITHFUL_ANSWER, strict_allowed_numbers(DOCX_FILE_CONTEXT, ()))
    assert "37.8" not in legacy

    fixed = _sanitize_preserving_analysis(FAITHFUL_ANSWER, _merged_strict(DOCX_FILE_CONTEXT))
    for token in ("37.8%", "NAR-7712", "QA_E2E_DOCX_20260710_NAR7712", "시사점"):
        assert token in fixed, token


def test_sanitize_still_blocks_numbers_absent_from_file_and_facts() -> None:
    fabricated = "Aurora 점수는 999.9입니다.\n\n승인코드는 NAR-7712입니다."
    out = _sanitize_preserving_analysis(fabricated, _merged_strict(DOCX_FILE_CONTEXT))
    assert "999.9" not in out
    assert "NAR-7712" in out


def test_markdown_messages_file_instruction_is_conditional() -> None:
    with_file = GenosClient._markdown_messages("질문", {"fact_md": ""}, "", DOCX_FILE_CONTEXT)
    without_file = GenosClient._markdown_messages("질문", {"fact_md": ""}, "", "")
    assert "원문 표기 그대로" in with_file[0]["content"]
    assert "부분 검색 컨텍스트만으로 정보가 없다고 단정하지 않는다" in with_file[0]["content"]
    assert "원문 표기 그대로" not in without_file[0]["content"]
    assert "업로드 파일 컨텍스트" in with_file[1]["content"]


def test_markdown_messages_require_each_file_in_multi_file_answers() -> None:
    messages = GenosClient._markdown_messages(
        "두 업로드 파일을 모두 사용해서 파일별로 비교해줘",
        {"fact_md": ""},
        "",
        MULTI_FILE_CONTEXT,
    )

    prompt = "\n".join(message["content"] for message in messages)
    assert "각 업로드 파일의 근거를 최소 1개씩" in prompt
    assert "파일별로 구분" in prompt


def test_multi_file_coverage_appends_grounded_evidence_for_every_file() -> None:
    answer = (
        "PDRN 파일에는 성별과 연령 변수가 있습니다.\n\n"
        "## 출처\n| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |"
    )

    covered = ensure_multi_file_evidence_coverage(
        "두 업로드 파일을 모두 사용해서 CVOT·LDL-C와 PDRN 성별·연령을 비교해줘",
        answer,
        MULTI_FILE_CONTEXT,
    )

    assert "## 파일별 근거 확인" in covered
    assert "pdrn_survey.xlsx" in covered
    assert "dyslipidemia_di.xlsx" in covered
    assert "성별 변수 SQ1" in covered
    assert "CVOT와 LDL-C" in covered
    assert covered.index("## 파일별 근거 확인") < covered.index("## 출처")


def test_single_file_answer_does_not_gain_multi_file_evidence_section() -> None:
    answer = "승인코드는 NAR-7712입니다."

    assert ensure_multi_file_evidence_coverage("이 파일을 요약해줘", answer, DOCX_FILE_CONTEXT) == answer


def test_markdown_messages_do_not_offer_legacy_mixed_synthesis() -> None:
    messages = GenosClient._markdown_messages(
        "리바로 매출과 이 보고서 전망을 비교해줘",
        {"fact_md": "", "context_scope": "MIXED"},
        "",
        DOCX_FILE_CONTEXT,
    )

    assert "두 구획으로 나눠" not in messages[0]["content"]
    assert "시장 데이터 기준" not in messages[0]["content"]


def test_warn_dropped_file_tokens_logs_warning(caplog) -> None:
    final_without_token = "**시사점 및 한계**\n단계적 롤아웃 방식이 권고되고 있습니다."
    with caplog.at_level(logging.WARNING, logger="jw_chat_agent_poc.service.genos_client"):
        _warn_dropped_file_tokens("질문", FAITHFUL_ANSWER, final_without_token, DOCX_FILE_CONTEXT)
    assert any("file-grounded tokens dropped" in record.message for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jw_chat_agent_poc.service.genos_client"):
        _warn_dropped_file_tokens("질문", FAITHFUL_ANSWER, FAITHFUL_ANSWER, DOCX_FILE_CONTEXT)
    assert not caplog.records


def test_absence_statement_assembled_when_target_missing_everywhere() -> None:
    question = "업로드 자료에 가상 브랜드 NOVA-ZETA-404의 매출이 있나? 없으면 자료에 없다고 명확히 답해."
    listing_only = "| 브랜드명 | 매출 |\n| --- | --- |\n| TESTROVA | 123.45 |"
    exhaustive_context = "검색 범위: 문서 전체 키워드 검색 + 벡터 검색\n\n" + DOCX_FILE_CONTEXT
    out = ensure_file_absence_statement(question, listing_only, exhaustive_context)
    assert out.startswith("업로드 문서에서 NOVA-ZETA-404을(를) 찾을 수 없습니다.")
    assert "TESTROVA" in out


def test_absence_statement_is_not_invented_from_partial_vector_context() -> None:
    question = "업로드 보고서에 31페이지 KOL 인용이 있나?"
    answer = "검색된 문단에는 시장 개요가 포함되어 있습니다."

    assert ensure_file_absence_statement(question, answer, DOCX_FILE_CONTEXT) == answer


def test_absence_statement_not_added_when_target_addressed_or_present() -> None:
    question = "업로드 보고서에 Project Eclipse Harbor의 처리시간 감소율이 있나?"
    addressed = "Project Eclipse Harbor 정보는 해당 문서에 포함되어 있지 않습니다."
    assert ensure_file_absence_statement(question, addressed, DOCX_FILE_CONTEXT) == addressed

    in_context = "업로드한 보고서에서 Project Dawn Beacon의 승인코드는 무엇이야?"
    answer = "승인코드는 NAR-7712입니다."
    assert ensure_file_absence_statement(in_context, answer, DOCX_FILE_CONTEXT) == answer

    # 파일 컨텍스트가 없으면(일반 질문) 어떤 조립도 하지 않는다
    assert ensure_file_absence_statement(question, "일반 답변", "") == "일반 답변"


def test_absence_statement_not_added_to_confirmed_sql_result() -> None:
    question = "BPI Numeric 시트에서 q1 값 1.0과 2.0 각각의 응답 수와 no 합계를 알려줘"
    answer = "| q1 | 응답 수 | no 합계 |\n| --- | --- | --- |\n| 1.0 | 690 | 2,679,529.0 |"
    sql_context = (
        "## 업로드 파일 SQL 결과\n"
        "파일: d2_bpi.xlsx\n"
        "시트: Numeric\n"
        "상태: 확인됨\n"
        "| q1 | COUNT(*) | SUM(no) |\n"
        "| --- | --- | --- |\n"
        "| 1.0 | 690 | 2679529.0 |"
    )

    assert ensure_file_absence_statement(question, answer, sql_context) == answer


def test_file_search_client_parses_file_source_items(monkeypatch) -> None:
    body = {
        "file_context": DOCX_FILE_CONTEXT,
        "file_sources": [
            {"document_id": 112706, "file_name": "qa_e2e_operations_brief.docx", "chunk_id": "c1"},
            {"document_id": 112706, "file_name": "qa_e2e_operations_brief.docx", "chunk_id": "c2"},
            {"document_id": 112705, "file_name": "qa_e2e_brand_sales.xlsx", "chunk_id": "c3"},
        ],
        "errors": [],
    }

    def fake_post(url, json=None, timeout=None):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr("jw_chat_agent_poc.service.file_search_client.requests.post", fake_post)
    result = search_uploaded_files("질문", "conv-1")
    assert result is not None
    assert result.file_source_items == (
        {"file_name": "qa_e2e_operations_brief.docx", "document_id": 112706},
        {"file_name": "qa_e2e_brand_sales.xlsx", "document_id": 112705},
    )


def test_file_search_client_preserves_public_location_provenance(monkeypatch) -> None:
    body = {
        "file_context": "[1] report.pdf\n[DA] report.pdf | p.7\n\n전망 1,200억원",
        "document_count": 1,
        "file_sources": [
            {
                "file_name": "report.pdf",
                "i_page": 7,
                "source_channel": "native_text",
            }
        ],
        "errors": [],
    }
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        ),
    )

    result = search_uploaded_files("전망", "conv-public-source")

    assert result is not None
    assert result.file_source_items == (
        {"file_name": "report.pdf", "i_page": 7, "source_channel": "native_text"},
    )


def test_file_search_client_preserves_active_session_when_search_times_out(monkeypatch) -> None:
    def timeout_post(url, json=None, timeout=None):
        raise requests.Timeout("search timeout")

    def documents_get(url, params=None, timeout=None):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"documents": [{"document_id": 112829, "file_name": "fixture.xlsx"}]},
        )

    monkeypatch.setattr("jw_chat_agent_poc.service.file_search_client.requests.post", timeout_post)
    monkeypatch.setattr("jw_chat_agent_poc.service.file_search_client.requests.get", documents_get)

    result = search_uploaded_files("업로드 파일의 리바로젯 항목", "conv-1")

    assert result is not None
    assert result.has_active_file is True
    assert result.file_context == ""
    assert result.errors == ("file search unavailable",)


def _final_answer(file_sources=()) -> FinalAnswer:
    return FinalAnswer(
        text="본문",
        charts=[],
        timing={},
        trace={},
        sources=("cache", "document"),
        conversation_id="conv-1",
        file_sources=tuple(file_sources),
    )


def test_sse_emits_file_sources_event_only_when_present() -> None:
    items = ({"file_name": "qa_e2e_operations_brief.docx", "document_id": 112706},)
    body = "".join(_sse_events_from_final_answer(_final_answer(items)))
    assert "event: file_sources" in body
    assert "qa_e2e_operations_brief.docx" in body
    assert '"document_id"' not in body
    sources_pos = body.index("event: sources")
    file_sources_pos = body.index("event: file_sources")
    assert sources_pos < file_sources_pos  # 기존 sources 이벤트 뒤에 추가만

    plain = "".join(_sse_events_from_final_answer(_final_answer()))
    assert "event: file_sources" not in plain
    assert "event: sources" in plain


class _EchoAgent:
    def __init__(self, *, external_mode: str = "live") -> None:
        self.external_mode = external_mode

    def answer(self, question: str, _documents=None) -> dict:
        return {"answer": f"ok:{question}", "sources": ["cache"], "tool_calls": []}


def _echo_factory(*, external_mode: str = "live") -> _EchoAgent:
    return _EchoAgent(external_mode=external_mode)


def test_chat_answer_returns_file_sources_end_to_end(monkeypatch) -> None:
    uploaded = UploadedFileSearchResult(
        file_context=DOCX_FILE_CONTEXT,
        file_sources=("qa_e2e_operations_brief.docx",),
        errors=(),
        file_source_items=({"file_name": "qa_e2e_operations_brief.docx", "document_id": 112706},),
    )
    monkeypatch.setattr(service_app, "search_uploaded_files", lambda question, conversation_id: uploaded)
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda conversation_id: True)
    client = TestClient(create_app(agent_factory=_echo_factory))

    response = client.post(
        "/chat/answer",
        json={"question": "이 문서에서 리바로 최근 실적 알려줘", "conversation_id": "conv-file"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["file_sources"] == [{"file_name": "qa_e2e_operations_brief.docx"}]
    assert "document" in payload["sources"]

    stream = client.get(
        "/chat/stream",
        params={"question": "이 문서에서 리바로 최근 실적 알려줘", "conversation_id": "conv-file"},
    )
    assert stream.status_code == 200
    assert "event: file_sources" in stream.text


def test_general_path_without_file_context_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(service_app, "search_uploaded_files", lambda question, conversation_id: None)
    client = TestClient(create_app(agent_factory=_echo_factory))

    response = client.post("/chat/answer", json={"question": "리바로 최근 실적 알려줘"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["file_sources"] == []
    assert "document" not in payload["sources"]

    stream = client.get("/chat/stream", params={"question": "리바로 최근 실적 알려줘"})
    assert "event: file_sources" not in stream.text
    assert "event: sources" in stream.text
