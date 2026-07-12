from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.orchestrator.provenance_facts import (
    provenance_row_from_file_context,
)
from jw_chat_agent_poc.service import file_sql_query
from jw_chat_agent_poc.service.file_search_client import search_uploaded_files
from jw_chat_agent_poc.service.file_sql_query import SqlFileSource


SQL_SOURCE = SqlFileSource(
    logical_name="doc-91:sheet-1",
    file_name="survey_raw.xlsx",
    sheet_name="Numeric",
    document_id=91,
)


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
            "sql": "SELECT c1, SUM(c2) AS total FROM data GROUP BY c1 ORDER BY c1",
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["c1", "total"],
            "rows": [["A", 30], ["B", 7]],
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
            "sql": "SELECT c2, COUNT(*), SUM(c1) FROM data GROUP BY c2",
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["c2", "COUNT(*)", "SUM(c1)"],
            "rows": [["1.0", 690, 2679529]],
        },
    )

    outcome = file_sql_query.query_uploaded_sql("q1별 응답 수와 no 합계", "conversation-1", (SQL_SOURCE,))

    assert "| q1 | COUNT(*) | SUM(no) |" in outcome.file_context
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


def test_sql_failure_is_fail_closed_with_explicit_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )

    outcome = file_sql_query.query_uploaded_sql("합계", "conversation-1", (SQL_SOURCE,))

    assert "확인할 수 없습니다" in outcome.file_context
    assert outcome.errors == ("file SQL query unavailable",)


def test_file_search_client_delegates_sql_sources_without_market_tools(monkeypatch) -> None:
    body = {
        "file_context": "",
        "document_count": 1,
        "file_sources": [],
        "sql_available": True,
        "sql_sources": [
            {
                "logical_name": SQL_SOURCE.logical_name,
                "file_name": SQL_SOURCE.file_name,
                "sheet_name": SQL_SOURCE.sheet_name,
                "document_id": SQL_SOURCE.document_id,
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
    monkeypatch.setattr(
        "jw_chat_agent_poc.service.file_search_client.query_uploaded_sql",
        lambda *args, **kwargs: file_sql_query.SqlQueryOutcome(
            file_context="## 업로드 파일 SQL 결과\n파일: survey_raw.xlsx\n| total |\n| --- |\n| 37 |",
            file_source_items=(
                {"file_name": "survey_raw.xlsx", "document_id": 91},
            ),
            errors=(),
        ),
    )

    result = search_uploaded_files("합계", "conversation-1")

    assert result is not None
    assert "SQL 결과" in result.file_context
    assert result.file_source_items == (
        {"file_name": "survey_raw.xlsx", "document_id": 91},
    )


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
