from __future__ import annotations

from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service import file_search_client, file_sql_query
from jw_chat_agent_poc.service.conversation import ConversationSlots, ConversationTurn
from jw_chat_agent_poc.service.conversation_context import extract_conversation_slots


def _wide_chso_schema() -> dict:
    columns = [
        {"query_name": f"c{index}", "source_name": f"UNRELATED METRIC {index}"}
        for index in range(1, 253)
    ]
    columns[1] = {"query_name": "c2", "source_name": "MFR NAME KOR"}
    columns[11] = {"query_name": "c12", "source_name": "ATC 4"}
    columns[71] = {
        "query_name": "c72",
        "source_name": "VALUES LC SI PRICE 1/2026",
    }
    columns[131] = {
        "query_name": "c132",
        "source_name": "SELL OUT PRICE AVERAGE 1/2026",
    }
    return {
        "logical_name": "doc-91:sheet-1",
        "file_name": "CHSO.xlsx",
        "sheet_name": "Sell Out Standard",
        "columns": columns,
    }


@pytest.mark.parametrize(
    "question",
    [
        "2026년 1월 총 sell-out 금액은?",
        "동아제약의 sell-out 합계는?",
        "동화약품의 합계는?",
        "동아제약과 동화약품 비교",
        "특정 ATC4에서 두 제조사 비교",
        "VALUES LC SI PRICE 1/2026 합계",
        "VALUES LC SI PRICE 1/2026 동아제약 합계",
        "VALUES LC SI PRICE 1/2026 동화약품 합계",
        "VALUES LC SI PRICE 1/2026 두 제조사 비교",
        "VALUES LC SI PRICE 1/2026 특정 ATC4 두 제조사 비교",
    ],
)
def test_chso_golden_questions_expose_c72_to_planner(monkeypatch, question: str) -> None:
    monkeypatch.setenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "160")

    compact = file_sql_query._compact_schema(question, _wide_chso_schema())

    assert "c72" in {column["query_name"] for column in compact["columns"]}
    if "금액" in question or "sell-out" in question.casefold():
        assert "c132" not in {column["query_name"] for column in compact["columns"]}


def test_natural_amount_schema_selection_is_deterministic(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "160")

    selections = [
        tuple(
            column["query_name"]
            for column in file_sql_query._compact_schema(
                "2026년 1월 총 sell-out 금액은?", _wide_chso_schema()
            )["columns"]
        )
        for _ in range(5)
    ]

    assert len(set(selections)) == 1
    assert "c72" in selections[0]
    assert "c132" not in selections[0]


def test_compact_schema_matches_query_name_and_reports_omissions(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "160")

    compact = file_sql_query._compact_schema("c72 합계", _wide_chso_schema())

    assert "c72" in {column["query_name"] for column in compact["columns"]}
    assert compact["schema_truncated"] is True
    assert compact["total_column_count"] == 252
    assert compact["omitted_column_count"] > 0
    assert "related columns may be omitted" in compact["selection_notice"]


def test_schema_cap_is_configurable_and_default_is_raised(monkeypatch) -> None:
    monkeypatch.delenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", raising=False)
    assert file_sql_query._max_schema_columns() > 160

    monkeypatch.setenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "224")
    assert file_sql_query._max_schema_columns() == 224


@pytest.mark.parametrize("term", ["총액", "금액", "총", "전체", "합"])
def test_aggregate_keywords_cover_natural_amount_terms(term: str) -> None:
    assert file_sql_query._is_aggregate_question(f"2026년 1월 sell-out {term}")


def test_single_character_aggregate_terms_do_not_match_inside_words() -> None:
    assert not file_sql_query._is_aggregate_question("적합한 행을 보여줘")


@pytest.mark.parametrize("question", ["금액 컬럼 목록", "전체 컬럼", "총 컬럼 수"])
def test_explicit_schema_intent_precedes_broad_aggregate_terms(question: str) -> None:
    assert file_sql_query._is_schema_question(question)


def test_amount_request_rejects_average_price_column(monkeypatch) -> None:
    schema = {
        "logical_name": "doc-91:sheet-1",
        "columns": [
            {"query_name": "c132", "source_name": "SELL OUT PRICE AVERAGE 1/2026"},
        ],
    }
    monkeypatch.setattr(file_sql_query, "_fetch_schema", lambda *args, **kwargs: schema)
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda *args, **kwargs: {
            "logical_name": "doc-91:sheet-1",
            "sql": "SELECT SUM(c132) AS total, COUNT(*) AS applied_rows FROM data",
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["total", "applied_rows"],
            "rows": [[12345, 100]],
        },
    )
    source = file_sql_query.SqlFileSource(
        "doc-91:sheet-1", "CHSO.xlsx", "Sell Out Standard"
    )

    outcome = file_sql_query.query_uploaded_sql(
        "2026년 1월 총 sell-out 금액은?", "conversation-1", (source,)
    )

    assert outcome.errors == ("file SQL selected column intent mismatch",)
    assert "금액 열을 찾지 못했습니다" in outcome.answer_md
    assert "12,345" not in outcome.answer_md


def test_amount_request_rejects_mixed_amount_and_average_targets() -> None:
    schema = _wide_chso_schema()
    sql = (
        "SELECT SUM(c72) AS amount, SUM(c132) AS average_amount, "
        "COUNT(*) AS applied_rows FROM data"
    )

    assert not file_sql_query._selected_columns_match_intent("amount", sql, schema)


def test_amount_request_rejects_avg_of_amount_column() -> None:
    schema = _wide_chso_schema()
    sql = "SELECT AVG(c72) AS total, COUNT(*) AS applied_rows FROM data"

    assert not file_sql_query._selected_columns_match_intent("amount", sql, schema)


def test_count_request_accepts_count_result_separate_from_applied_rows() -> None:
    sql = (
        "SELECT COUNT(*) AS total_count, COUNT(*) AS applied_rows "
        "FROM data"
    )

    assert file_sql_query._selected_columns_match_intent("count", sql, _wide_chso_schema())


def test_explicit_filename_filters_search_context_and_sql_sources(monkeypatch) -> None:
    body = {
        "file_context": (
            "[1] F5.pptx (document_id=105)\nF5 only text\n\n"
            "[2] F4.docx (document_id=104)\nF4 only text"
        ),
        "document_count": 2,
        "file_sources": [
            {"document_id": 105, "file_name": "F5.pptx"},
            {"document_id": 104, "file_name": "F4.docx"},
        ],
        "sql_available": True,
        "sql_sources": [
            {"logical_name": "doc-105", "file_name": "F5.pptx", "sheet_name": "data"},
            {"logical_name": "doc-104", "file_name": "F4.docx", "sheet_name": "data"},
        ],
        "errors": [],
    }
    monkeypatch.setattr(
        file_search_client.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: body
        ),
    )
    captured = []

    def fake_sql(question, conversation_id, sources):
        captured.extend(sources)
        return file_sql_query.SqlQueryOutcome("", (), ())

    monkeypatch.setattr(file_search_client, "query_uploaded_sql", fake_sql)

    result = file_search_client.search_uploaded_files(
        "F4에서 핵심 수치를 알려줘", "conversation-1"
    )

    assert result is not None
    assert "F4 only text" in result.file_context
    assert "F5 only text" not in result.file_context
    assert result.file_source_items == ({"file_name": "F4.docx", "document_id": 104},)
    assert [source.file_name for source in captured] == ["F4.docx"]


def test_explicit_filename_fails_closed_when_target_is_missing_from_hits(monkeypatch) -> None:
    body = {
        "file_context": "[1] F5.pptx (document_id=105)\nF5 only text",
        "document_count": 2,
        "file_sources": [{"document_id": 105, "file_name": "F5.pptx"}],
        "errors": [],
    }
    monkeypatch.setattr(
        file_search_client.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: body
        ),
    )

    result = file_search_client.search_uploaded_files(
        "F4.docx에서 핵심 수치를 알려줘", "conversation-1"
    )

    assert result is not None
    assert result.file_context == ""
    assert result.file_source_items == ()


def test_filename_stem_does_not_match_inside_unrelated_word() -> None:
    sources = [{"file_name": "F4.docx"}]

    assert file_search_client._requested_file_names("XF4 분석", sources) == frozenset()
    assert file_search_client._requested_file_names("F4에서 분석", sources) == frozenset(
        {"f4.docx"}
    )


def test_file_followup_inherits_previous_aggregate_and_filename() -> None:
    previous = ConversationTurn(
        question="F4.docx에서 동아제약 합계는?",
        answer="21,978,584,141",
        applied_filters={},
        slots=ConversationSlots(
            file_name="F4.docx",
            file_measure="VALUES LC SI PRICE 1/2026",
        ),
    )

    resolved = service_app._resolve_file_question("동화약품은?", previous)

    assert "F4.docx" in resolved
    assert "동화약품" in resolved
    assert "합계" in resolved
    assert "VALUES LC SI PRICE 1/2026" in resolved


def test_market_anchor_does_not_inherit_file_slots(monkeypatch) -> None:
    previous = ConversationTurn(
        question="F4.docx에서 동아제약 합계는?",
        answer="21,978,584,141",
        applied_filters={},
        slots=ConversationSlots(
            file_name="F4.docx",
            file_measure="VALUES LC SI PRICE 1/2026",
        ),
    )
    store = service_app.SessionStore()
    store.conversations.record_exchange(
        "phase2-market-return",
        previous.question,
        previous.answer,
        slots=previous.slots,
    )
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda *_args: True)
    monkeypatch.setattr(
        service_app,
        "_delegated_file_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("MARKET must not search files")
        ),
    )
    monkeypatch.setattr(
        service_app,
        "_answer_with_conversation",
        lambda *_args, **_kwargs: {"answer": "시장 답변", "tool_calls": [], "sources": []},
    )

    item = service_app._answer_question(
        store,
        service_app.MarketScopeResolver(),
        lambda: None,
        "리바로 최근 매출 추이",
        "live",
        "phase2-market-return",
    )

    assert item["result"]["context_scope"] == "MARKET"


def test_file_sql_result_records_measure_for_next_turn() -> None:
    slots = extract_conversation_slots(
        {
            "deterministic_file_answer": (
                "## 업로드 파일 집계 결과\n"
                "파일: CHSO.xlsx\n"
                "사용 열: VALUES LC SI PRICE\n1/2026\n"
                "집계 함수: SUM, COUNT\n"
                "적용 행 수: 12,268"
            )
        }
    )

    assert slots.file_name == "CHSO.xlsx"
    assert slots.file_measure == "VALUES LC SI PRICE 1/2026"


@pytest.mark.parametrize(
    ("question", "rows", "expected"),
    [
        ("2026년 1월 총 sell-out 금액은?", [[386933825518, 12268]], ("386,933,825,518",)),
        ("VALUES LC SI PRICE 1/2026 합계", [[386933825518, 12268]], ("386,933,825,518",)),
        ("동아제약의 sell-out 합계는?", [[21978584141, 348]], ("21,978,584,141",)),
        ("VALUES LC SI PRICE 1/2026 동아제약 합계", [[21978584141, 348]], ("21,978,584,141",)),
        ("동화약품의 sell-out 합계는?", [[15188575523, 208]], ("15,188,575,523",)),
        ("VALUES LC SI PRICE 1/2026 동화약품 합계", [[15188575523, 208]], ("15,188,575,523",)),
        (
            "동아제약과 동화약품 비교",
            [["동아제약", 21978584141, 348], ["동화약품", 15188575523, 208]],
            ("21,978,584,141", "15,188,575,523", "6,790,008,618"),
        ),
        (
            "VALUES LC SI PRICE 1/2026 두 제조사 비교",
            [["동아제약", 21978584141, 348], ["동화약품", 15188575523, 208]],
            ("21,978,584,141", "15,188,575,523", "6,790,008,618"),
        ),
        (
            "특정 ATC4에서 두 제조사 비교",
            [["동화약품", 3853883875, 120], ["동아제약", 3315233364, 98]],
            ("3,853,883,875", "3,315,233,364"),
        ),
        (
            "VALUES LC SI PRICE 1/2026 특정 ATC4 두 제조사 비교",
            [["동화약품", 3853883875, 120], ["동아제약", 3315233364, 98]],
            ("3,853,883,875", "3,315,233,364"),
        ),
    ],
)
def test_chso_natural_and_explicit_golden_answers(question, rows, expected) -> None:
    comparison = len(rows[0]) == 3
    sql = (
        "SELECT c2, SUM(c72) AS total_value, COUNT(*) AS applied_rows "
        "FROM data GROUP BY c2"
        if comparison
        else "SELECT SUM(c72) AS total_value, COUNT(*) AS applied_rows FROM data"
    )
    columns = ["c2", "total_value", "applied_rows"] if comparison else ["total_value", "applied_rows"]
    answer = file_sql_query._render_aggregate_answer(
        question,
        file_sql_query.SqlFileSource("doc-91", "CHSO.xlsx", "Sell Out Standard"),
        sql,
        {"columns": columns, "rows": rows},
        {
            "columns": [
                {"query_name": "c2", "source_name": "MFR NAME KOR"},
                {"query_name": "c72", "source_name": "VALUES LC SI PRICE 1/2026"},
            ]
        },
    )

    assert "VALUES LC SI PRICE 1/2026" in answer
    assert "SELL OUT PRICE AVERAGE" not in answer
    for value in expected:
        assert value in answer
