"""R14 — period span resolution, measure disclosure, and total-row exclusion.

The defect these lock down: a question naming only a year resolved to no month
keys at all, so the planner silently aggregated the single newest column and
presented it as the year's figure.
"""
from __future__ import annotations

import re

import pytest

from jw_chat_agent_poc.common import periods as P
from jw_chat_agent_poc.service import file_sql_query as F


DIMENSIONS = (
    "AUDIT DESC", "MFR NAME KOR", "PRODUCT NAME KOR", "PACK DESCRIPTION",
    "CHC 1", "CHC 2", "CHC 3", "CHC 4", "ATC 1", "ATC 2", "ATC 3", "ATC 4",
)
MEASURES = ("VALUES LC SI PRICE", "UNITS", "SELL OUT PRICE AVERAGE", "SELL IN PRICE")


def _month_labels(count: int = 60, year: int = 2021, month: int = 2) -> list[str]:
    labels = []
    for _ in range(count):
        labels.append(f"{month}/{year}")
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return labels


@pytest.fixture(name="schema")
def _schema() -> dict:
    headers = list(DIMENSIONS)
    for measure in MEASURES:
        headers.extend(f"{measure}\n{label}" for label in _month_labels())
    return {
        "logical_name": "chso",
        "file_name": "CHSO.xlsx",
        "sheet_name": "Sell Out  Standard",
        "query_table": "data",
        "columns": [
            {"source_name": header, "query_name": f"c{index + 1}"}
            for index, header in enumerate(headers)
        ],
    }


def _plan(schema: dict, question: str):
    return F._resolve_deterministic_select(question, (schema,))


def _summed_columns(sql: str) -> list[str]:
    return re.findall(r"SUM\((c\d+)\)", sql)


# --- period primitives ----------------------------------------------------

def test_bare_year_is_recognised_even_though_it_is_not_a_month_key():
    assert P.month_keys("2025년 매출") == frozenset()
    assert P.explicit_years("2025년 매출") == (2025,)


def test_year_with_a_month_is_not_also_read_as_a_bare_year():
    assert P.explicit_years("2024년 3월 매출") == ()
    assert P.month_keys("2024년 3월 매출") == frozenset({"2024-03"})


def test_quarter_expands_to_its_three_months():
    assert P.quarter_months("2025-Q3") == ("2025-07", "2025-08", "2025-09")


def test_relative_span_counts_back_from_the_anchor_inclusive():
    assert P.months_back("2026-01", 3) == ("2025-11", "2025-12", "2026-01")
    assert P.relative_span("최근 3년 추이") == (3, "년")
    assert P.relative_span("최근 6달 추이") == (6, "개월")


def test_months_back_stops_at_the_start_of_the_calendar_instead_of_wrapping():
    assert P.months_back("0000-02", 5) == ("0000-01", "0000-02")


# --- span resolution ------------------------------------------------------

def test_a_year_resolves_to_all_twelve_of_its_months(schema):
    resolution = _plan(schema, "2025년 박카스디 매출 알려줘")
    assert resolution.plan is not None
    assert resolution.period.status == "resolved"
    assert resolution.period.months == tuple(
        f"2025-{month:02d}" for month in range(1, 13)
    )
    assert len(_summed_columns(resolution.plan["sql"])) == 12


def test_a_span_the_workbook_cannot_serve_is_refused_not_substituted(schema):
    resolution = _plan(schema, "2019년 매출 상위 10개 제품")
    assert resolution.plan is None
    assert resolution.period.status == "unresolved"
    assert "2019년" in " ".join(resolution.missing_slots)


def test_a_partly_available_span_reports_what_was_dropped(schema):
    resolution = _plan(schema, "2026년 제품별 매출 상위 5개")
    assert resolution.period.status == "partial"
    assert resolution.period.months == ("2026-01",)
    assert len(resolution.period.missing) == 11


def test_an_unstated_period_covers_the_whole_workbook(schema):
    resolution = _plan(schema, "제조사별 매출 합계")
    assert resolution.period.status == "full_span"
    assert len(resolution.period.months) == 60
    assert len(_summed_columns(resolution.plan["sql"])) == 60


def test_a_quarter_resolves_to_its_three_months(schema):
    resolution = _plan(schema, "2025년 3분기 제조사별 매출 합계")
    assert resolution.period.months == ("2025-07", "2025-08", "2025-09")


def test_a_relative_span_anchors_on_the_newest_available_month(schema):
    resolution = _plan(schema, "최근 3년 제조사별 매출 합계")
    assert resolution.period.months[0] == "2023-02"
    assert resolution.period.months[-1] == "2026-01"
    assert len(resolution.period.months) == 36


# --- measure selection ----------------------------------------------------

def test_amount_is_the_default_and_says_so(schema):
    resolution = _plan(schema, "제조사별 매출 합계")
    assert resolution.metric.family == F.METRIC_AMOUNT
    lines = F._scope_disclosure_lines(resolution.period, resolution.metric)
    assert any("지표" in line for line in lines)


def test_a_quantity_question_selects_the_units_block(schema):
    resolution = _plan(schema, "2025년 제조사별 수량 합계")
    assert resolution.metric.family == F.METRIC_QUANTITY
    assert resolution.metric.defaulted is False
    summed = _summed_columns(resolution.plan["sql"])
    by_query = {
        column["query_name"]: column["source_name"] for column in schema["columns"]
    }
    assert all(by_query[name].startswith("UNITS") for name in summed)


def test_a_quantity_plan_still_satisfies_the_selected_column_intent_gate(schema):
    resolution = _plan(schema, "2025년 제조사별 수량 합계")
    assert F._selected_columns_match_intent(
        "quantity", resolution.plan["sql"], schema
    )


def test_a_multi_month_amount_plan_still_satisfies_the_intent_gate(schema):
    resolution = _plan(schema, "2025년 OTC 매출 상위 10개 제품")
    sql = resolution.plan["sql"]
    assert F._selected_columns_match_intent("amount", sql, schema)
    assert F._has_aggregate_contract(sql)


def test_the_answer_states_the_span_and_the_measure_it_used(schema):
    resolution = _plan(schema, "2025년 OTC 매출 상위 10개 제품")
    lines = F._scope_disclosure_lines(resolution.period, resolution.metric)
    joined = "\n".join(lines)
    assert "2025-01~2025-12" in joined
    assert "12개월" in joined


def test_the_published_evidence_block_states_the_span_too(schema):
    """The document lane publishes _render_result, not the aggregate answer.
    A market-scope reader sees this block, so the span must be in it."""
    resolution = _plan(schema, "2025년 박카스디 매출 알려줘")
    source = F.SqlFileSource(
        logical_name="chso", file_name="CHSO.xlsx", sheet_name="Sell Out  Standard"
    )
    rendered = F._render_result(
        source,
        {"columns": ["c3", "total_value", "applied_rows"],
         "rows": [["박카스디", 79286925800, 4]]},
        schema,
        period=resolution.period,
        metric=resolution.metric,
    )
    assert "2025-01~2025-12" in rendered
    assert "지표" in rendered
    assert not re.search(r"(?<![A-Za-z])c\d{1,3}(?![A-Za-z0-9])", rendered.split("|")[0])


def test_the_filter_description_never_exposes_internal_column_names(schema):
    """The WHERE clause is written against query names (c3). Printing it
    verbatim leaked them, and the total-row rule added ten more per aggregate."""
    resolution = _plan(schema, "액티넘이엑스골드 2024년 매출 알려줘")
    source = F.SqlFileSource(
        logical_name="chso", file_name="CHSO.xlsx", sheet_name="Sell Out  Standard"
    )
    rendered = F._render_aggregate_answer(
        "액티넘이엑스골드 2024년 매출 알려줘",
        source,
        resolution.plan["sql"],
        {"columns": ["c3", "total_value", "applied_rows"],
         "rows": [["액티넘이엑스골드", 672932000, 2]]},
        schema,
        period=resolution.period,
        metric=resolution.metric,
    )
    assert not re.search(r"(?<![A-Za-z0-9_])c\d+(?![A-Za-z0-9_])", rendered)
    assert "PRODUCT NAME KOR" in rendered


def test_the_total_row_rule_is_not_spelled_out_in_the_filter_line(schema):
    resolution = _plan(schema, "제조사별 매출 합계")
    filtered = F._public_filter_text(
        resolution.plan["sql"].split("WHERE", 1)[1], schema
    )
    assert "COALESCE" not in filtered
    assert "TRIM" not in filtered


def test_a_defaulted_measure_is_labelled_as_defaulted(schema):
    scope = F.MetricScope(
        family=F.METRIC_AMOUNT, label="금액", defaulted=True, columns=()
    )
    lines = F._scope_disclosure_lines(None, scope)
    assert any("기본값" in line for line in lines)


# --- total-row exclusion --------------------------------------------------

def test_a_row_with_no_identity_at_all_is_excluded_from_aggregation(schema):
    predicate = F._aggregate_row_exclusion(schema["columns"])
    assert predicate
    # AUDIT DESC describes the row rather than identifying it, so a workbook
    # total that only fills that column must not survive the predicate.
    assert "c1" not in re.findall(r"(c\d+)", predicate)
    assert "c2" in re.findall(r"(c\d+)", predicate)


def test_the_exclusion_reaches_the_emitted_sql(schema):
    resolution = _plan(schema, "제조사별 매출 합계")
    assert "COALESCE(TRIM(c2), '') <> ''" in resolution.plan["sql"]
    assert "total_row_exclusion" in resolution.resolved_slots


def test_the_exclusion_is_disclosed_rather_than_applied_silently():
    lines = F._scope_disclosure_lines(None, None, excluded_total_rows=True)
    assert any("집계 제외" in line for line in lines)


def test_a_workbook_without_identity_columns_gets_no_exclusion():
    columns = [{"source_name": "VALUES\n1/2025", "query_name": "c1"}]
    assert F._aggregate_row_exclusion(columns) == ""


# --- trace ----------------------------------------------------------------

def test_the_trace_records_how_the_span_was_decided(schema):
    resolution = _plan(schema, "2025년 OTC 매출 상위 10개 제품")
    fields = F._period_trace_fields(resolution.period, resolution.metric)
    assert fields["period_status"] == "resolved"
    assert fields["period_span"] == "2025-01~2025-12"
    assert fields["period_month_count"] == "12"
    assert fields["metric_family"] == "amount"


def test_a_refused_span_records_its_reason_instead_of_going_quiet(schema):
    resolution = _plan(schema, "2019년 매출 상위 10개 제품")
    fields = F._period_trace_fields(resolution.period, resolution.metric)
    assert fields["period_status"] == "unresolved"
    assert fields["period_reason"] == "requested_period_absent"


def test_internal_column_names_never_reach_the_reader(schema):
    resolution = _plan(schema, "2025년 OTC 매출 상위 10개 제품")
    surface = "\n".join(
        F._scope_disclosure_lines(resolution.period, resolution.metric)
    )
    assert not re.search(r"\bc\d+\b", surface)


def test_header_newlines_do_not_break_the_used_column_line():
    text = F._column_list_text(["VALUES LC SI PRICE\n1/2026", "ATC 4"])
    assert "\n" not in text


def test_a_wide_span_summarises_its_columns_instead_of_listing_all(schema):
    names = [f"VALUES LC SI PRICE\n{month}/2025" for month in range(1, 13)]
    text = F._column_list_text(names)
    assert "외 6개" in text
