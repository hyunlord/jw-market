from __future__ import annotations

from types import SimpleNamespace

import pytest
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


def _data_row_count_query_only(
    _conversation_id: str,
    _logical_name: str,
    sql: str,
) -> dict[str, object]:
    assert sql == "SELECT COUNT(*) AS data_row_count FROM data"
    return {
        "columns": ["data_row_count"],
        "rows": [[12_268]],
        "row_count": 1,
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
                {"query_name": "c12", "source_name": "ATC 4"},
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
                ["동화약품", 3853883875, 22],
                ["동아제약", 3315233364, 17],
            ],
        },
    )

    outcome = file_sql_query.query_uploaded_sql(
        "ATC4 R05A0에서 동화약품과 동아제약을 비교해줘",
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


def test_existing_atc4_column_with_no_matching_value_reports_zero_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c2", "source_name": "MFR NAME KOR"},
                {"query_name": "c12", "source_name": "ATC 4"},
                {"query_name": "c72", "source_name": "VALUES LC SI PRICE 1/2026"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *_args: {
            "columns": ["c2", "total_value", "applied_rows"],
            "rows": [],
        },
    )

    outcome = file_sql_query.query_uploaded_sql(
        "ATC4 A02B2에서 동아제약과 동화약품의 sell-out 금액 비교",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert outcome.status == "no_matching_rows"
    assert "ATC4 열은 있으나 'A02B2' 조건에 맞는 행이 0건입니다" in outcome.answer_md
    assert "sell-out을(를) 찾을 수 없습니다" not in outcome.answer_md
    assert "ATC4 관련 열이 없습니다" not in outcome.answer_md
    assert outcome.trace[-1] == {
        "stage": "render",
        "status": "no_matching_rows",
        "filter": "ATC4=A02B2",
    }


def test_real_zero_value_is_not_misclassified_as_no_matching_rows() -> None:
    assert file_sql_query._has_no_applied_rows(
        {"columns": ["total_value", "applied_rows"], "rows": [[0, 1]]}
    ) is False
    assert file_sql_query._has_no_applied_rows(
        {"columns": ["total_value", "applied_rows"], "rows": [[None, 0]]}
    ) is True


def test_aggregate_comparison_concludes_when_question_asks_which_is_larger(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *args, **kwargs: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c2", "source_name": "MFR NAME KOR"},
                {"query_name": "c72", "source_name": "VALUES LC SI PRICE 1/2026"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_generate_select",
        lambda question, schemas: {
            "logical_name": SQL_SOURCE.logical_name,
            "sql": (
                "SELECT c2, SUM(c72) AS total_value, COUNT(*) AS applied_rows "
                "FROM data WHERE c2 IN ('동아제약','동화약품') GROUP BY c2"
            ),
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {
            "columns": ["c2", "total_value", "applied_rows"],
            "rows": [
                ["동아제약", 21978584141, 348],
                ["동화약품", 15188575523, 208],
            ],
        },
    )

    outcome = file_sql_query.query_uploaded_sql(
        (
            "2026년 1월 VALUES LC SI PRICE를 제조사별로 합산했을 때 "
            "동아제약과 동화약품 중 어디가 더 크고 각각 얼마야?"
        ),
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "비교 결론: 동아제약" in outcome.answer_md
    assert "6,790,008,618" in outcome.answer_md


def test_aggregate_intent_covers_natural_language_sum_comparison() -> None:
    question = (
        "2026년 1월 VALUES LC SI PRICE를 제조사별로 합산했을 때 "
        "동아제약과 동화약품 중 어디가 더 크고 각각 얼마야?"
    )

    assert file_sql_query._is_aggregate_question(question) is True


def test_manufacturer_by_sum_builds_grouped_deterministic_query() -> None:
    schema = {
        "logical_name": SQL_SOURCE.logical_name,
        "columns": [
            {"query_name": "c2", "source_name": "MFR NAME KOR"},
            {
                "query_name": "c72",
                "source_name": "VALUES LC SI PRICE 1/2026",
            },
        ],
    }

    resolution = file_sql_query._resolve_deterministic_select(
        "제조사별 합계",
        (schema,),
    )

    assert resolution.missing_slots == ()
    assert resolution.resolved_slots == ("measure", "manufacturer")
    assert resolution.plan == {
        "logical_name": SQL_SOURCE.logical_name,
        "sql": (
            "SELECT c2, SUM(c72) AS total_value, COUNT(*) AS applied_rows "
            "FROM data GROUP BY c2 ORDER BY total_value DESC"
        ),
    }


def test_manufacturer_by_sum_executes_grouped_rows_without_total_fallback(
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c2", "source_name": "MFR NAME KOR"},
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )

    def run_query(_conversation_id: str, _logical_name: str, sql: str):
        captured["sql"] = sql
        return {
            "columns": ["c2", "total_value", "applied_rows"],
            "rows": [
                ["동아제약", 21_978_584_141, 348],
                ["동화약품", 15_188_575_523, 208],
            ],
        }

    monkeypatch.setattr(file_sql_query, "_run_query", run_query)

    outcome = file_sql_query.query_uploaded_sql(
        "제조사별 합계",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "GROUP BY c2 ORDER BY total_value DESC" in captured["sql"]
    assert "동아제약" in outcome.answer_md
    assert "21,978,584,141" in outcome.answer_md
    assert "동화약품" in outcome.answer_md
    assert "15,188,575,523" in outcome.answer_md
    assert "386,933,825,518" not in outcome.answer_md


def test_channel_by_count_builds_grouped_deterministic_query() -> None:
    schema = {
        "logical_name": SQL_SOURCE.logical_name,
        "columns": [
            {"query_name": "c259", "source_name": "채널"},
            {"query_name": "c1", "source_name": "응답자 번호"},
        ],
    }

    resolution = file_sql_query._resolve_deterministic_select(
        "채널별 건수",
        (schema,),
    )

    assert resolution.missing_slots == ()
    assert resolution.resolved_slots == ("measure", "channel")
    assert resolution.plan == {
        "logical_name": SQL_SOURCE.logical_name,
        "sql": (
            "SELECT c259, COUNT(*) AS response_count, COUNT(*) AS applied_rows "
            "FROM data GROUP BY c259 ORDER BY response_count DESC"
        ),
    }


def test_channel_by_count_executes_grouped_rows(monkeypatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c259", "source_name": "채널"},
                {"query_name": "c1", "source_name": "응답자 번호"},
            ],
        },
    )

    def run_query(_conversation_id: str, _logical_name: str, sql: str):
        captured["sql"] = sql
        return {
            "columns": ["c259", "response_count", "applied_rows"],
            "rows": [["2", 100, 100], ["1", 92, 92]],
        }

    monkeypatch.setattr(file_sql_query, "_run_query", run_query)

    outcome = file_sql_query.query_uploaded_sql(
        "채널별 건수",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "GROUP BY c259 ORDER BY response_count DESC" in captured["sql"]
    assert "| 2 | 100 | 100 |" in outcome.answer_md
    assert "| 1 | 92 | 92 |" in outcome.answer_md
    assert "채널 2: 100건" in outcome.answer_md
    assert "채널 1: 92건" in outcome.answer_md


def test_fastest_growing_channel_compares_grounded_period_endpoints() -> None:
    schema = {
        "logical_name": SQL_SOURCE.logical_name,
        "columns": [
            {"query_name": "c5", "source_name": "CHANNEL"},
            {
                "query_name": "c70",
                "source_name": "VALUES LC SI PRICE 11/2025",
            },
            {
                "query_name": "c71",
                "source_name": "VALUES LC SI PRICE 12/2025",
            },
            {
                "query_name": "c72",
                "source_name": "VALUES LC SI PRICE 1/2026",
            },
        ],
    }

    resolution = file_sql_query._resolve_deterministic_select(
        "가장 성장한 채널은",
        (schema,),
    )

    assert resolution.missing_slots == ()
    assert resolution.resolved_slots == ("channel", "growth_periods")
    assert resolution.plan == {
        "logical_name": SQL_SOURCE.logical_name,
        "sql": (
            "SELECT c5, SUM(c70) AS period_2025_11, "
            "SUM(c72) AS period_2026_01, "
            "(SUM(c72) - SUM(c70)) AS growth_value, "
            "COUNT(*) AS applied_rows FROM data "
            "WHERE c5 IS NOT NULL AND TRIM(c5) <> '' "
            "GROUP BY c5 ORDER BY growth_value DESC"
        ),
    }
    assert file_sql_query._is_select_only_candidate(resolution.plan["sql"])


def test_fastest_growing_channel_renders_ranked_grounded_narrative(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c5", "source_name": "CHANNEL"},
                {
                    "query_name": "c70",
                    "source_name": "VALUES LC SI PRICE 11/2025",
                },
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *_args: {
            "columns": [
                "c5",
                "period_2025_11",
                "period_2026_01",
                "growth_value",
                "applied_rows",
            ],
            "rows": [
                ["병원", 100, 150, 50, 6],
                ["의원", 100, 120, 20, 8],
            ],
        },
    )

    outcome = file_sql_query.query_uploaded_sql(
        "가장 성장한 채널은",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "| 채널 | 2025-11 | 2026-01 | 증가액 | 적용 행 수 |" in outcome.answer_md
    assert "2025-11 대비 2026-01 절대 증가액 기준" in outcome.answer_md
    assert "가장 성장한 채널은 병원이며 증가액은 50입니다" in outcome.answer_md
    assert "때문" not in outcome.answer_md


def test_monthly_trend_builds_one_select_over_ordered_month_columns() -> None:
    schema = {
        "logical_name": SQL_SOURCE.logical_name,
        "columns": [
            {"query_name": "c2", "source_name": "MFR NAME KOR"},
            {
                "query_name": "c70",
                "source_name": "VALUES LC SI PRICE 11/2025",
            },
            {
                "query_name": "c71",
                "source_name": "VALUES LC SI PRICE 12/2025",
            },
            {
                "query_name": "c72",
                "source_name": "VALUES LC SI PRICE 1/2026",
            },
        ],
    }

    resolution = file_sql_query._resolve_deterministic_select(
        "월별 추이",
        (schema,),
    )

    assert resolution.missing_slots == ()
    assert resolution.resolved_slots == ("monthly_measures",)
    assert resolution.plan == {
        "logical_name": SQL_SOURCE.logical_name,
        "sql": (
            "SELECT SUM(c70) AS period_2025_11, "
            "SUM(c71) AS period_2025_12, "
            "SUM(c72) AS period_2026_01, "
            "COUNT(*) AS applied_rows FROM data"
        ),
    }


def test_monthly_trend_without_month_columns_fails_closed() -> None:
    schema = {
        "logical_name": SQL_SOURCE.logical_name,
        "columns": [
            {"query_name": "c2", "source_name": "MFR NAME KOR"},
            {"query_name": "c5", "source_name": "SALES AMOUNT"},
        ],
    }

    resolution = file_sql_query._resolve_deterministic_select(
        "월별 추이",
        (schema,),
    )

    assert resolution.plan is None
    assert resolution.missing_slots == ("월별 금액 열",)


def test_manufacturer_monthly_trend_filters_and_renders_ordered_narrative(
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c2", "source_name": "MFR NAME KOR"},
                {
                    "query_name": "c70",
                    "source_name": "VALUES LC SI PRICE 11/2025",
                },
                {
                    "query_name": "c71",
                    "source_name": "VALUES LC SI PRICE 12/2025",
                },
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )

    def run_query(_conversation_id: str, _logical_name: str, sql: str):
        captured["sql"] = sql
        return {
            "columns": [
                "period_2025_11",
                "period_2025_12",
                "period_2026_01",
                "applied_rows",
            ],
            "rows": [[18_000_000_000, 20_000_000_000, 21_978_584_141, 348]],
        }

    monkeypatch.setattr(file_sql_query, "_run_query", run_query)

    outcome = file_sql_query.query_uploaded_sql(
        "동아제약의 월별 합계",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "WHERE c2 = '동아제약'" in captured["sql"]
    assert "| 기간 | 합계 |" in outcome.answer_md
    assert outcome.answer_md.index("| 2025-11 |") < outcome.answer_md.index(
        "| 2025-12 |"
    ) < outcome.answer_md.index("| 2026-01 |")
    assert "18,000,000,000에서 21,978,584,141로 증가했습니다" in outcome.answer_md
    assert "사용 열: VALUES LC SI PRICE 11/2025" in outcome.answer_md


def test_named_column_sum_is_not_misclassified_as_schema_inspection() -> None:
    question = "VALUES LC SI PRICE 1/2026 컬럼의 전체 합계를 계산해줘."

    assert file_sql_query._is_aggregate_question(question) is True
    assert file_sql_query._is_schema_question(question) is False


def test_wide_schema_keeps_identity_columns_before_keyword_matches() -> None:
    schema = {
        "logical_name": SQL_SOURCE.logical_name,
        "file_name": SQL_SOURCE.file_name,
        "sheet_name": SQL_SOURCE.sheet_name,
        "columns": [
            {"query_name": f"c{index}", "source_name": f"VALUES LC SI PRICE {index}/2026"}
            for index in range(1, 253)
        ],
    }
    schema["columns"][1] = {"query_name": "c2", "source_name": "MFR NAME KOR"}
    schema["columns"][11] = {"query_name": "c12", "source_name": "ATC 4"}
    schema["columns"][71] = {"query_name": "c72", "source_name": "VALUES LC SI PRICE 1/2026"}

    compact = file_sql_query._compact_schema(
        (
            "2026년 1월 ATC 4가 R05A0_COLD PREPARATIONS인 행 중 "
            "동화약품과 동아제약의 VALUES LC SI PRICE 합계를 각각 비교해줘."
        ),
        schema,
    )

    names = {column["query_name"] for column in compact["columns"]}
    assert {"c2", "c12", "c72"} <= names


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
        "_resolve_deterministic_select",
        lambda question, schemas: file_sql_query.DeterministicPlanResolution(
            {
                "logical_name": SQL_SOURCE.logical_name,
                "sql": "SELECT SUM(c72) AS total FROM data",
            },
            ("measure",),
        ),
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


def test_file_overview_describes_grounded_dimensions_and_measures(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "MFR NAME KOR"},
                {"query_name": "c2", "source_name": "PRODUCT NAME KOR"},
                {
                    "query_name": "c71",
                    "source_name": "VALUES LC SI PRICE 12/2025",
                },
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        _data_row_count_query_only,
    )

    outcome = file_sql_query.query_uploaded_sql(
        "이 파일에 뭐가 있어",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "주요 차원 열: MFR NAME KOR, PRODUCT NAME KOR" in outcome.answer_md
    assert (
        "측정 열: VALUES LC SI PRICE 12/2025, VALUES LC SI PRICE 1/2026"
        in outcome.answer_md
    )


def test_sellout_measure_explanation_names_only_available_source_columns(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "MFR NAME KOR"},
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        _data_row_count_query_only,
    )

    outcome = file_sql_query.query_uploaded_sql(
        "셀아웃 지표 설명해줘",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert (
        "파일에서 확인된 셀아웃 측정 열: VALUES LC SI PRICE 1/2026"
        in outcome.answer_md
    )
    assert "질문에 지정한 기간의 실제 열을 선택해 집계합니다" in outcome.answer_md


def test_period_overview_reports_measured_range_and_source_columns(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {
                    "query_name": "c70",
                    "source_name": "VALUES LC SI PRICE 11/2025",
                },
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        _data_row_count_query_only,
    )

    outcome = file_sql_query.query_uploaded_sql(
        "어떤 기간 데이터야",
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert "기간 범위: 2025-11 ~ 2026-01" in outcome.answer_md
    assert "기간 근거 열: VALUES LC SI PRICE 11/2025, VALUES LC SI PRICE 1/2026" in outcome.answer_md


@pytest.mark.parametrize("question", ["분석해줘", "이거 어때"])
def test_ambiguous_file_question_asks_with_schema_grounded_options(
    monkeypatch,
    question: str,
) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": SQL_SOURCE.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "MFR NAME KOR"},
                {"query_name": "c2", "source_name": "PRODUCT NAME KOR"},
                {
                    "query_name": "c71",
                    "source_name": "VALUES LC SI PRICE 12/2025",
                },
                {
                    "query_name": "c72",
                    "source_name": "VALUES LC SI PRICE 1/2026",
                },
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *_args: (_ for _ in ()).throw(AssertionError("SQL must not run")),
    )

    outcome = file_sql_query.query_uploaded_sql(
        question,
        "conversation-1",
        (SQL_SOURCE,),
    )

    assert outcome.status == "clarification_needed"
    assert "어떤 분석을 원하시나요?" in outcome.answer_md
    assert "MFR NAME KOR별" in outcome.answer_md
    assert "VALUES LC SI PRICE 1/2026" in outcome.answer_md
    assert "월별 추이" in outcome.answer_md


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
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_data_row_count",
        lambda source, conversation_id: {
            "doc:questions": 14,
            "doc:sources": 26,
            "doc:criteria": 8,
        }[source.logical_name],
        raising=False,
    )

    outcome = file_sql_query.query_uploaded_sql("이 엑셀 파일 구조를 요약해줘", "conversation-1", sources)

    assert "시트 수: 3개" in outcome.answer_md
    assert "질문 수: 14개" in outcome.answer_md
    assert "출처 수: 26개" in outcome.answer_md
    assert "384행" not in outcome.answer_md


def test_workbook_structure_reports_sql_data_rows_not_physical_sheet_rows(monkeypatch) -> None:
    source = SqlFileSource(
        "doc:chso",
        "chso.xlsx",
        "Sell Out Standard",
        row_count=12_269,
        column_count=252,
    )
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: {
            "logical_name": source.logical_name,
            "columns": [
                {"query_name": "c1", "source_name": "MFR NAME KOR"},
                {"query_name": "c2", "source_name": "VALUES LC SI PRICE 1/2026"},
            ],
        },
    )
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_data_row_count",
        lambda *_args: 12_268,
        raising=False,
    )

    outcome = file_sql_query.query_uploaded_sql(
        "이 파일에 뭐가 있어",
        "conversation-1",
        (source,),
    )

    assert "데이터 행 수: 12,268" in outcome.answer_md
    assert "데이터 행 수: 12,269" not in outcome.answer_md


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
        "_resolve_deterministic_select",
        lambda question, schemas: file_sql_query.DeterministicPlanResolution(
            {
                "logical_name": SQL_SOURCE.logical_name,
                "sql": "SELECT c1 FROM data WHERE c1 = 'missing'",
            },
            ("requested_value",),
        ),
    )
    monkeypatch.setattr(
        file_sql_query,
        "_run_query",
        lambda *args, **kwargs: {"columns": ["c1"], "rows": []},
    )

    outcome = file_sql_query.query_uploaded_sql("없는 값", "conversation-1", (SQL_SOURCE,))

    assert "상태: 조건 일치 0건" in outcome.file_context
    assert "요청한 조건에 맞는 행이 0건입니다" in outcome.answer_md
    assert outcome.status == "no_matching_rows"
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


def test_file_schema_probe_uses_public_sql_source_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        file_search_client.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"sql_sources": [PUBLIC_FILE_SQL_SOURCE]},
        ),
    )
    captured: list[SqlFileSource] = []

    def fetch_columns(conversation_id, sources):
        assert conversation_id == "conversation-1"
        captured.extend(sources)
        return ("ATC 4", "MFR NAME KOR")

    monkeypatch.setattr(file_search_client, "fetch_sql_schema_columns", fetch_columns)

    columns = file_search_client.fetch_uploaded_file_schema_columns("conversation-1")

    assert columns == ("ATC 4", "MFR NAME KOR")
    assert len(captured) == 1
    assert captured[0].document_id is None


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
