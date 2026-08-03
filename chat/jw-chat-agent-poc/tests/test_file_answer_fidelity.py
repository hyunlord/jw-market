from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import requests
from fastapi.testclient import TestClient

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import FinalAnswer, _sse_events_from_final_answer, create_app
from jw_chat_agent_poc.service.file_search_client import (
    UploadedFileSearchResult,
    fetch_uploaded_file_overviews,
    search_uploaded_files,
)
from jw_chat_agent_poc.service.genos_client import (
    GenosClient,
    _sanitize_preserving_analysis,
    _warn_dropped_file_tokens,
)
from jw_chat_agent_poc.service.answer_safety import (
    answer_has_only_fact_numbers,
    ensure_cross_file_comparison_judgment,
    ensure_file_overview_evidence_coverage,
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


def test_file_overview_answer_preserves_retrieved_summary_sections(monkeypatch) -> None:
    def stream_answer(_client, _question, _result):
        yield "업로드 파일 기준으로 이 보고서는 질환 배경과 치료 현황을 설명합니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    question = "이 보고서 핵심 내용을 요약해줘"
    result = {
        "context_scope": "FILE",
        "answer": "업로드 파일에서 확인된 근거만 사용해 답변합니다.",
        "sources": ["document"],
        "tool_calls": [],
        "markdown_response": {"markdown": "", "fact_md": "", "data_md": ""},
        "file_context": (
            "[1] portfolio.pdf (document_id=41) (page=2)\n"
            "섹션: Key Takeaways\n"
            "# Key Takeaways\n"
            "- The market faces increasing biosimilar competition.\n"
            "- Oral therapies carry a boxed safety warning.\n\n"
            "[2] portfolio.pdf (document_id=41) (page=9)\n"
            "섹션: Unmet Needs\n"
            "# Unmet Needs\n"
            "Safer durable remission remains an unmet need."
        ),
        "file_source_items": [
            {"file_name": "portfolio.pdf", "i_page": 2, "source_channel": "native_text"},
            {"file_name": "portfolio.pdf", "i_page": 9, "source_channel": "native_text"},
        ],
    }

    final = service_app.compute_final_answer(question, result, "overview-answer")

    assert "biosimilar competition" in final.text
    assert "boxed safety warning" in final.text
    assert "unmet need" in final.text


def test_presentation_overview_preserves_named_analysis_sections(monkeypatch) -> None:
    def stream_answer(_client, _question, _result):
        yield "이 발표는 브랜드 경쟁력을 진단하는 분석 체계를 제안합니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    question = "이 발표 핵심 메시지를 알려줘"
    result = {
        "context_scope": "FILE",
        "answer": "업로드 파일에서 확인된 근거만 사용해 답변합니다.",
        "sources": ["document"],
        "tool_calls": [],
        "markdown_response": {"markdown": "", "fact_md": "", "data_md": ""},
        "file_context": (
            "[1] strategy.pptx (document_id=52) (slide=4)\n"
            "섹션: Market Landscape\n"
            "# Market Landscape\n"
            "Competitive position by brand.\n\n"
            "[2] strategy.pptx (document_id=52) (slide=12)\n"
            "섹션: Market Size\n"
            "# Market Size\n"
            "Growth Contribution and HHI are tracked together."
        ),
        "file_source_items": [
            {"file_name": "strategy.pptx", "slide_number": 4, "source_channel": "native_text"},
            {"file_name": "strategy.pptx", "slide_number": 12, "source_channel": "native_text"},
        ],
    }

    final = service_app.compute_final_answer(question, result, "presentation-overview")

    for token in ("Market Landscape", "Market Size", "Growth Contribution", "HHI"):
        assert token in final.text


def test_file_overview_coverage_does_not_change_detail_questions() -> None:
    answer = "승인 약물은 Polaris입니다."
    context = (
        "[1] portfolio.pdf (document_id=41) (page=2)\n"
        "# Key Takeaways\n"
        "The market faces increasing biosimilar competition."
    )

    assert ensure_file_overview_evidence_coverage("승인 약물 이름이 뭐야", answer, context) == answer


def test_file_overview_heading_mention_does_not_count_as_body_coverage() -> None:
    answer = "주요 섹션으로 Key Takeaways가 있습니다."
    context = (
        "[1] portfolio.pdf (document_id=41) (page=2)\n"
        "# Key Takeaways\n"
        "The market faces increasing biosimilar competition.\n"
        "Oral therapies carry a boxed safety warning."
    )

    covered = ensure_file_overview_evidence_coverage("이 문서 요약해줘", answer, context)

    assert "biosimilar competition" in covered
    assert "boxed safety warning" in covered


def test_cross_file_judgment_does_not_reject_comparable_metrics() -> None:
    answer = "보고서와 엑셀 모두 Revenue를 같은 통화 단위로 제시합니다."
    context = (
        "[1] portfolio.pdf (document_id=41) (page=2)\n"
        "Revenue by month\n\n"
        "## 업로드 파일 SQL 결과\n"
        "파일: revenue.xlsx\n"
        "| period | revenue |\n"
        "| --- | ---: |\n"
        "| current | 42 |"
    )

    assert ensure_cross_file_comparison_judgment("두 파일의 Revenue를 비교해줘", answer, context) == answer


def test_single_file_answer_does_not_gain_multi_file_evidence_section() -> None:
    answer = "승인코드는 NAR-7712입니다."

    assert ensure_multi_file_evidence_coverage("이 파일을 요약해줘", answer, DOCX_FILE_CONTEXT) == answer


def test_cross_file_sql_and_document_question_synthesizes_both_evidence_types(monkeypatch) -> None:
    calls = 0

    def stream_answer(_client, question, result):
        nonlocal calls
        calls += 1
        assert question == "RA 보고서의 승인 약물 표와 엑셀 수치를 비교해 일치 여부를 알려줘"
        assert "Simponi" in result["file_context"]
        assert "386,933,825,518" in result["file_context"]
        yield (
            "RA 보고서에는 승인 약물 Simponi가 제시되어 있고, "
            "엑셀의 2026년 1월 sell-out 합계는 386,933,825,518입니다."
        )

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    question = "RA 보고서의 승인 약물 표와 엑셀 수치를 비교해 일치 여부를 알려줘"
    result = {
        "context_scope": "FILE",
        "answer": "업로드 파일에서 확인된 근거만 사용해 답변합니다.",
        "sources": ["document", "file_upload"],
        "tool_calls": [],
        "markdown_response": {"markdown": "", "fact_md": "", "data_md": ""},
        "file_context": (
            "[1] primary.pdf (document_id=113889) (page=20)\n"
            "Approved drug table: Simponi\n\n"
            "## 업로드 파일 SQL 결과\n"
            "파일: chso.xlsx\n"
            "| total_value | applied_rows |\n"
            "| --- | --- |\n"
            "| 386,933,825,518 | 12,268 |"
        ),
        "file_source_items": [
            {"file_name": "primary.pdf", "i_page": 20, "source_channel": "native_text"},
            {"file_name": "chso.xlsx", "sheet_name": "Sell Out Standard"},
        ],
        "deterministic_file_answer": (
            "## 업로드 파일 집계 결과\n"
            "파일: chso.xlsx\n"
            "| total_value | applied_rows |\n"
            "| --- | --- |\n"
            "| 386,933,825,518 | 12,268 |"
        ),
    }

    final = service_app.compute_final_answer(question, result, "cross-file-answer")

    assert calls == 1
    assert "Simponi" in final.text
    assert "386,933,825,518" in final.text
    assert "직접적인 일치 여부를 판정할 수 없습니다" in final.text
    assert {source["file_name"] for source in final.file_sources} == {"primary.pdf", "chso.xlsx"}


def test_single_file_comparison_keeps_deterministic_sql_answer(monkeypatch) -> None:
    def fail_if_streamed(_client, _question, _result):
        raise AssertionError("single-file comparison must keep the deterministic SQL path")

    monkeypatch.setattr(GenosClient, "stream_answer", fail_if_streamed)
    question = "동아와 동화 제조사별 합계를 비교해줘"
    deterministic_answer = (
        "## 업로드 파일 집계 결과\n"
        "파일: chso.xlsx\n"
        "| 제조사 | 합계 |\n"
        "| --- | ---: |\n"
        "| 동아 | 10 |\n"
        "| 동화 | 8 |"
    )
    result = {
        "context_scope": "FILE",
        "answer": "업로드 파일에서 확인된 근거만 사용해 답변합니다.",
        "sources": ["file_upload"],
        "tool_calls": [],
        "markdown_response": {"markdown": "", "fact_md": "", "data_md": ""},
        "file_context": deterministic_answer,
        "file_source_items": [
            {"file_name": "chso.xlsx", "sheet_name": "Sell Out Standard"},
        ],
        "deterministic_file_answer": deterministic_answer,
    }

    final = service_app.compute_final_answer(question, result, "single-file-comparison")

    assert "| 동아 | 10 |" in final.text
    assert "| 동화 | 8 |" in final.text
    assert {source["file_name"] for source in final.file_sources} == {"chso.xlsx"}


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

    def fake_post(url, json=None, headers=None, timeout=None):
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
        "file_context": "[1] brief.pptx\n전망 1,200억원",
        "document_count": 1,
        "file_sources": [
            {
                "file_name": "brief.pptx",
                "i_page": 7,
                "slide_number": 7,
                "section_title": "시장 전망",
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
        {
            "file_name": "brief.pptx",
            "i_page": 7,
            "slide_number": 7,
            "section_title": "시장 전망",
            "source_channel": "native_text",
        },
    )


def test_file_search_client_prioritizes_key_takeaways_for_document_summary(monkeypatch) -> None:
    body = {
        "file_context": (
            "[1] primary.pdf (page=3)\n"
            "Disease Background\n\n"
            "[2] primary.pdf (page=2)\n"
            "Key Takeaways\n"
            "Biosimilar competition and unmet need for safer drugs.\n\n"
            "[3] primary.pdf (page=18)\n"
            "Marketed and Pipeline Drugs"
        ),
        "document_count": 1,
        "file_sources": [{"file_name": "primary.pdf", "i_page": 3}],
        "errors": [],
    }
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        ),
    )

    result = search_uploaded_files("이 문서 요약해줘", "conv-summary")
    conclusion_result = search_uploaded_files("이 보고서의 결론이 뭐야", "conv-conclusion")

    assert result is not None
    assert result.file_context.startswith("[2] primary.pdf (page=2)\nKey Takeaways")
    assert result.file_context.count("[1] primary.pdf") == 1
    assert result.file_context.count("[2] primary.pdf") == 1
    assert result.file_context.count("[3] primary.pdf") == 1
    assert result.file_context.index("[1] primary.pdf") < result.file_context.index("[3] primary.pdf")
    assert conclusion_result is not None
    assert conclusion_result.file_context.startswith("[2] primary.pdf (page=2)\nKey Takeaways")


def test_file_search_client_prioritizes_substantive_overview_over_title_only_block(monkeypatch) -> None:
    body = {
        "file_context": (
            "[1] primary.pdf (page=2)\n"
            "Key Takeaways\n"
            "Disease Analysis\n\n"
            "[2] primary.pdf (page=2)\n"
            "Key Takeaways\n"
            "The market faces biosimilar competition and persistent unmet need.\n"
            "Three evidence-backed conclusions support the document-level thesis.\n\n"
            "[3] primary.pdf (page=49)\n"
            "Future Trends\n"
            "A specific pipeline asset is discussed."
        ),
        "document_count": 1,
        "file_sources": [{"file_name": "primary.pdf", "i_page": 2}],
        "errors": [],
    }
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        ),
    )

    result = search_uploaded_files("이 문서 요약해줘", "conv-substantive-summary")

    assert result is not None
    assert result.file_context.startswith("[2] primary.pdf (page=2)\nKey Takeaways")
    assert result.file_context.count("primary.pdf") == 3


def test_file_search_client_supplements_truncated_conclusion_context(monkeypatch) -> None:
    questions: list[str] = []

    def search_response(url, json=None, headers=None, timeout=None):
        question = str((json or {}).get("question") or "")
        questions.append(question)
        context = (
            "[1] primary.pdf (page=49)\nFuture Trends\nA pipeline asset is discussed.\n\n"
            "[2] primary.pdf (page=2)\nKey Takeaways\n- A"
        )
        if "unmet needs" in question:
            context = (
                "[1] primary.pdf (page=2)\nKey Takeaways\n"
                "There remains an unmet need for safer and cost-effective therapies."
            )
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "file_context": context,
                "document_count": 1,
                "file_sources": [{"file_name": "primary.pdf", "i_page": 2}],
                "errors": [],
            },
        )

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        search_response,
    )

    result = search_uploaded_files("이 보고서의 결론이 뭐야", "conv-truncated-conclusion")

    assert result is not None
    assert len(questions) == 2
    assert "unmet needs" in questions[1]
    assert "There remains an unmet need" in result.file_context
    assert result.file_context.count("There remains an unmet need") == 1


def test_file_search_client_supplements_general_document_summary_context(monkeypatch) -> None:
    questions: list[str] = []

    def search_response(url, json=None, headers=None, timeout=None):
        question = str((json or {}).get("question") or "")
        questions.append(question)
        context = (
            "[1] primary.pdf (page=4)\nDisease Background\n"
            "The disease is chronic and progressive."
        )
        if "key takeaways" in question:
            context = (
                "[1] primary.pdf (page=2)\nKey Takeaways\n"
                "Biosimilar competition is increasing.\n"
                "Oral therapies carry a boxed safety warning."
            )
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "file_context": context,
                "document_count": 1,
                "file_sources": [{"file_name": "primary.pdf", "i_page": 2}],
                "errors": [],
            },
        )

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        search_response,
    )

    result = search_uploaded_files("이 문서 요약해줘", "conv-general-summary")

    assert result is not None
    assert len(questions) == 2
    assert "key takeaways" in questions[1]
    assert "Biosimilar competition is increasing" in result.file_context
    assert "boxed safety warning" in result.file_context


def test_markdown_messages_require_document_level_overview_before_background() -> None:
    context = (
        "[1] primary.pdf (page=2)\nKey Takeaways\nMarket conclusion.\n\n"
        "[2] primary.pdf (page=9)\nDisease Background\nDefinition."
    )

    messages = GenosClient._markdown_messages(
        "이 보고서의 결론이 뭐야",
        {"fact_md": ""},
        "",
        context,
    )

    prompt = "\n".join(message["content"] for message in messages)
    assert "문서 전체 수준의 요약·결론·미충족 수요" in prompt
    assert "개별 질환 배경이나 단일 표보다 먼저" in prompt


def test_file_search_client_keeps_retrieval_order_for_specific_file_question(monkeypatch) -> None:
    calls = 0
    context = (
        "[1] primary.pdf (page=3)\n"
        "Disease Background\n\n"
        "[2] primary.pdf (page=2)\n"
        "Key Takeaways"
    )
    body = {
        "file_context": context,
        "document_count": 1,
        "file_sources": [{"file_name": "primary.pdf", "i_page": 3}],
        "errors": [],
    }
    def search_response(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        )

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        search_response,
    )

    result = search_uploaded_files("3페이지의 질환 정의를 알려줘", "conv-detail")

    assert result is not None
    assert calls == 1
    assert result.file_context == context


def test_file_search_client_preserves_active_session_when_search_times_out(monkeypatch) -> None:
    def timeout_post(url, json=None, headers=None, timeout=None):
        raise requests.Timeout("search timeout")

    def documents_get(url, params=None, headers=None, timeout=None):
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


def test_uploaded_file_overviews_preserve_public_sql_shape(monkeypatch) -> None:
    def documents_get(url, params=None, headers=None, timeout=None):
        assert url.endswith("/documents")
        assert params["chat_id"] == "conv-card"
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "documents": [
                    {
                        "file_name": "CHSO.xlsx",
                        "storage_route": "hybrid",
                        "chunk_count": 18,
                        "file_card": {
                            "file_name": "CHSO.xlsx",
                            "file_type": "xlsx",
                            "size_bytes": 16_000_000,
                            "title": "CHSO Sell Out",
                            "sheet_count": 1,
                            "sheets": [
                                {
                                    "name": "Sell Out Standard",
                                    "row_count": 12_269,
                                    "column_count": 252,
                                }
                            ],
                        },
                        "sql_tables": [
                            {
                                "logical_name": "data_chso",
                                "sheet_name": "Sell Out Standard",
                                "row_count": 12_269,
                                "column_count": 252,
                            }
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.get",
        documents_get,
    )

    overviews = fetch_uploaded_file_overviews("conv-card")

    assert len(overviews) == 1
    assert overviews[0].file_name == "CHSO.xlsx"
    assert overviews[0].storage_route == "hybrid"
    assert overviews[0].sql_tables[0].sheet_name == "Sell Out Standard"
    assert overviews[0].sql_tables[0].row_count == 12_269
    assert overviews[0].sql_tables[0].column_count == 252
    assert overviews[0].title == "CHSO Sell Out"
    assert overviews[0].sheet_count == 1
    assert overviews[0].sheets[0].name == "Sell Out Standard"


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


def test_sse_source_labels_keep_each_uploaded_file_visible() -> None:
    items = (
        {"file_name": "sales_january.xlsx", "document_id": 101},
        {"file_name": "sales_february.xlsx", "document_id": 202},
    )

    body = "".join(_sse_events_from_final_answer(_final_answer(items)))
    sources_event = body.split("event: sources\ndata: ", 1)[1].split("\n\n", 1)[0]

    assert "업로드 문서" in sources_event
    assert "sales_january.xlsx" in sources_event
    assert "sales_february.xlsx" in sources_event
    assert "101" not in sources_event
    assert "202" not in sources_event


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
