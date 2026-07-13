from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.orchestrator.provenance_facts import (
    provenance_row_from_file_context,
)
from jw_chat_agent_poc.service import file_sql_query
from jw_chat_agent_poc.service import file_search_client
from jw_chat_agent_poc.service.file_search_client import search_uploaded_files
from jw_chat_agent_poc.service.file_sql_query import SqlFileSource


SQL_SOURCE = SqlFileSource(
    logical_name="doc-91:sheet-1",
    file_name="survey_raw.xlsx",
    sheet_name="Numeric",
    document_id=91,
)

PUBLIC_FILE_SQL_SOURCE = {
    "logical_name": "doc-91:sheet-1",
    "sheet_name": "Numeric",
    "row_count": 12269,
    "column_count": 252,
    "file_name": "survey_raw.xlsx",
}


def test_sql_source_is_queried_and_rendered_as_file_context(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: {
            "logical_name": "doc-91:sheet-1",
            "query_table": "data",
            "columns": [
                {"query_name": "c1", "source_name": "brand"},
                {"query_name": "c2", "source_name": "sales"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda question, schemas: {
            "logical_name": "doc-91:sheet-1",
            "sql": (
                "SELECT c1, SUM(c2) AS total, COUNT(*) AS applied_rows "
                "FROM data GROUP BY c1 ORDER BY c1"
            ),
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["c1", "total", "applied_rows"],
            "rows": [["A", 30, 2], ["B", 7, 1]],
        },
    )

    outcome = file_sql_query.query_uploaded_sql(
        "브랜드별 매출 합계",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert outcome.errors == ()
    assert "## 업로드 파일 SQL 결과" in outcome.file_context
    assert "파일: survey_raw.xlsx" in outcome.file_context
    assert "시트: Numeric" in outcome.file_context
    assert "| A | 30 |" in outcome.file_context


def test_aggregate_contract_requires_numbers_rows_and_comparison_conclusion(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "MFR NAME KOR"},
                {"query_name": "c72", "source_name": "1/2026 VALUES LC SI PRICE"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda question, schemas: {
            "logical_name": SQL_SOURCE.logical_name,
            "sql": (
                "SELECT c1, SUM(c72) AS total_value, COUNT(*) AS applied_rows "
                "FROM data WHERE c1 IN ('동화약품','동아제약') GROUP BY c1 ORDER BY c1"
            ),
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["c1", "total_value", "applied_rows"],
            "rows": [
                ["동화약품", 3853883875, 120],
                ["동아제약", 3315233364, 98],
            ],
        },
    )

    outcome = file_sql_query.query_uploaded_sql(
        "ATC4 조건에서 동화약품과 동아제약을 비교해줘",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert outcome.answer_md
    assert "필터 조건" in outcome.answer_md
    assert "사용 열" in outcome.answer_md
    assert "SUM" in outcome.answer_md
    assert "적용 행 수" in outcome.answer_md
    assert "3,853,883,875" in outcome.answer_md
    assert "3,315,233,364" in outcome.answer_md
    assert "538,650,511" in outcome.answer_md
    assert "동화약품" in outcome.answer_md and "더 큽니다" in outcome.answer_md


def test_aggregate_without_applied_rows_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [{"query_name": "c72", "source_name": "sales"}],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda question, schemas: {
            "logical_name": SQL_SOURCE.logical_name,
            "sql": "SELECT SUM(c72) AS total FROM data",
        },
    )

    outcome = file_sql_query.query_uploaded_sql("총 합계", "conversation-1", (SQL_SOURCE,))

    assert outcome.errors == ("file SQL aggregate contract unavailable",)
    assert "확인할 수 없습니다" in outcome.answer_md


def test_schema_question_uses_measured_schema_without_planner(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "MFR NAME KOR"},
                {"query_name": "c2", "source_name": "ATC 4"},
                {"query_name": "c71", "source_name": "12/2025 VALUES LC SI PRICE"},
                {"query_name": "c72", "source_name": "1/2026 VALUES LC SI PRICE"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planner must not run")),
    )

    outcome = file_sql_query.query_uploaded_sql(
        "제조사, ATC4, 월별 value 열과 마지막 월을 알려줘",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "MFR NAME KOR" in outcome.answer_md
    assert "ATC 4" in outcome.answer_md
    assert "1/2026 VALUES LC SI PRICE" in outcome.answer_md
    assert "마지막 월: 1/2026" in outcome.answer_md
    assert "2/2026 열: 없음" in outcome.answer_md


def test_workbook_structure_uses_only_measured_sheet_and_row_counts(monkeypatch) -> None:
    sources = (
        SqlFileSource("doc:questions", "questions.xlsx", "질문", row_count=14, column_count=4),
        SqlFileSource("doc:sources", "questions.xlsx", "Sources", row_count=26, column_count=3),
        SqlFileSource("doc:criteria", "questions.xlsx", "평가기준", row_count=8, column_count=6),
    )
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda source, conversation_id: {
            "logical_name": source.logical_name,
            "columns": [{"query_name": "c1", "source_name": "기준값 384"}],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("planner must not run")),
    )

    outcome = file_sql_query.query_uploaded_sql("이 엑셀 파일 구조를 요약해줘", "conversation-1", sources)

    assert "시트 수: 3개" in outcome.answer_md
    assert "질문 수: 14개" in outcome.answer_md
    assert "출처 수: 26개" in outcome.answer_md
    assert "384행" not in outcome.answer_md


def test_query_headers_use_original_source_column_names(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "no"},
                {"query_name": "c2", "source_name": "q1"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda question, schemas: {
            "logical_name": SQL_SOURCE.logical_name,
            "sql": (
                "SELECT c2, COUNT(*) AS row_count, SUM(c1), "
                "COUNT(*) AS applied_rows FROM data GROUP BY c2"
            ),
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["c2", "COUNT(*)", "SUM(c1)", "applied_rows"],
            "rows": [["1.0", 690, 2679529, 690]],
        },
    )

    outcome = file_sql_query.query_uploaded_sql("q1별 응답 수와 no 합계", "conversation-1", (SQL_SOURCE,))

    assert "| q1 | COUNT(*) | SUM(no) | applied_rows |" in outcome.file_context
    assert "| c2 |" not in outcome.file_context


def test_planner_default_output_budget_covers_reasoning_models(monkeypatch) -> None:
    monkeypatch.delenv("JW_CHAT_FILE_SQL_PLANNER_MAX_TOKENS", raising=False)

    assert file_sql_query._planner_max_tokens() == 2048


def test_planner_prompt_declares_uploaded_cell_text_affinity(monkeypatch) -> None:
    monkeypatch.delenv("JW_CHAT_FILE_SQL_PLANNER_SYSTEM_PROMPT", raising=False)

    prompt = file_sql_query._planner_system_prompt()

    assert "TEXT affinity" in prompt
    assert "quoted string literals" in prompt


def test_planner_prompt_uses_aggregates_supported_by_scoped_sql_policy(monkeypatch) -> None:
    monkeypatch.delenv("JW_CHAT_FILE_SQL_PLANNER_SYSTEM_PROMPT", raising=False)

    prompt = file_sql_query._planner_system_prompt()

    assert "SUM and AVG directly" in prompt
    assert "Never use CAST" in prompt


def test_session_payload_preserves_workflow_and_both_session_aliases(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_FILE_WORKFLOW_ID", "301")

    payload = file_sql_query._session_payload("conversation-owned", logical_name="doc-91:sheet-1")

    assert payload == {
        "workflow_id": 301,
        "app_session_id": "conversation-owned",
        "chat_id": "conversation-owned",
        "logical_name": "doc-91:sheet-1",
    }


def test_zero_rows_are_explicit_not_silent(monkeypatch) -> None:
    monkeypatch.setattr(file_sql_query, "_fetch_schema", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda question, schemas: {
            "logical_name": SQL_SOURCE.logical_name,
            "sql": "SELECT c1 FROM data WHERE c1 = 'missing'",
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {"columns": ["c1"], "rows": []},
    )

    outcome = file_sql_query.query_uploaded_sql("없는 값", "conversation-1", (SQL_SOURCE,))

    assert "원천 조회 결과 0행" in outcome.file_context
    assert "시장" not in outcome.file_context


def test_sql_failure_is_fail_closed_with_explicit_unavailable(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )

    outcome = file_sql_query.query_uploaded_sql("합계", "conversation-1", (SQL_SOURCE,))

    assert "확인할 수 없습니다" in outcome.file_context
    assert outcome.errors == ("file SQL query unavailable",)
    assert "file SQL query failed" in caplog.text
    assert "down" in caplog.text


def test_public_file_sql_source_contract_does_not_require_document_id() -> None:
    sources = file_search_client._sql_sources([PUBLIC_FILE_SQL_SOURCE])

    assert len(sources) == 1
    assert sources[0].logical_name == PUBLIC_FILE_SQL_SOURCE["logical_name"]
    assert sources[0].document_id is None


def test_invalid_sql_source_is_logged_without_discarding_valid_sources(caplog) -> None:
    sources = file_search_client._sql_sources(
        [
            {"file_name": "broken.xlsx"},
            PUBLIC_FILE_SQL_SOURCE,
        ]
    )

    assert len(sources) == 1
    assert "discarding invalid file SQL source" in caplog.text
    assert "logical_name" in caplog.text


def test_file_search_client_delegates_sql_sources_without_market_tools(monkeypatch) -> None:
    body = {
        "file_context": "",
        "document_count": 1,
        "file_sources": [],
        "sql_available": True,
        "sql_sources": [PUBLIC_FILE_SQL_SOURCE],
        "errors": [],
    }
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        ),
    )
    captured_sources = []

    def fake_query_uploaded_sql(question, conversation_id, sources):
        captured_sources.extend(sources)
        return file_sql_query.SqlQueryOutcome(
            file_context="## 업로드 파일 SQL 결과\n파일: survey_raw.xlsx\n| total |\n| --- |\n| 37 |",
            file_source_items=(
                {"file_name": "survey_raw.xlsx"},
            ),
            errors=(),
        )

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.query_uploaded_sql",
        fake_query_uploaded_sql,
    )

    result = search_uploaded_files("합계", "conversation-1")

    assert result is not None
    assert "SQL 결과" in result.file_context
    assert result.file_source_items == (
        {"file_name": "survey_raw.xlsx"},
    )
    assert len(captured_sources) == 1
    assert captured_sources[0].document_id is None


def test_sql_provenance_uses_uploaded_filename_and_missing_public_labels() -> None:
    row = provenance_row_from_file_context(
        "## 업로드 파일 SQL 결과\n파일: survey_raw.xlsx\n시트: Numeric\n| total |\n| --- |\n| 37 |"
    )

    assert row is not None
    assert row.source == "업로드 파일(survey_raw.xlsx)"
    assert row.view == "—"
    assert row.market == "—"


def test_sql_provenance_uses_sql_filename_in_mixed_file_context() -> None:
    row = provenance_row_from_file_context(
        "[1] existing_vdb.pdf\n기존 검색 문맥\n\n"
        "## 업로드 파일 SQL 결과\n"
        "파일: survey_raw.xlsx\n"
        "시트: Numeric\n"
        "| total |\n| --- |\n| 37 |"
    )

    assert row is not None
    assert row.source == "업로드 파일(survey_raw.xlsx)"
