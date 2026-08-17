from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

import requests

from jw_chat_agent_poc.service.actor_context import code_serving_actor_headers

from jw_chat_agent_poc.common.periods import (
    explicit_years,
    month_keys,
    months_back,
    quarter_keys,
    quarter_months,
    relative_span,
    year_months,
)
from jw_chat_agent_poc.orchestrator.unavailable_response import file_absence_answer


logger = logging.getLogger(__name__)


DEFAULT_AGGREGATE_TERMS = (
    "합계",
    "총계",
    "합산",
    "평균",
    "개수",
    "건수",
    "몇 개",
    "집계",
    "비교",
    "대비",
    "총액",
    "금액",
    "총",
    "전체",
    "합",
    "COUNT",
    "SUM",
    "AVG",
)
DEFAULT_AMOUNT_QUESTION_TERMS = ("금액", "총액", "매출", "sell-out", "sell out", "sales", "amount")
DEFAULT_AMOUNT_COLUMN_TERMS = ("values lc si price", "sales", "amount", "revenue", "매출", "금액")
DEFAULT_AVERAGE_TERMS = ("average", "avg", "평균", "단가", "unit price")
DEFAULT_COUNT_QUESTION_TERMS = ("개수", "건수", "몇 개", "count")
DEFAULT_QUANTITY_TERMS = ("quantity", "qty", "volume", "수량")
# ``UNITS`` is the IQVIA-style header for a count measure. It is matched
# separately from DEFAULT_QUANTITY_TERMS so the shared term list keeps its
# existing meaning for every other caller.
_UNIT_COLUMN_RE = re.compile(r"(?<![A-Za-z])units?(?![A-Za-z])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SqlFileSource:
    logical_name: str
    file_name: str
    sheet_name: str
    document_id: int | None = None
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True, slots=True)
class SqlQueryOutcome:
    file_context: str
    file_source_items: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    answer_md: str = ""
    status: str = "ok"
    trace: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class MeasureRequest:
    state: str
    intent: str | None = None
    label: str = ""


@dataclass(frozen=True, slots=True)
class DeterministicPlanResolution:
    plan: dict[str, str] | None
    resolved_slots: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    period: "PeriodScope | None" = None
    metric: "MetricScope | None" = None


# Metric families a wide period-per-column workbook can expose. The family is
# derived from the header text, never from column position, so a workbook that
# orders its blocks differently resolves the same way.
METRIC_AMOUNT: Final = "amount"
METRIC_AVERAGE: Final = "average"
METRIC_QUANTITY: Final = "quantity"

_METRIC_LABELS: Final[Mapping[str, str]] = {
    METRIC_AMOUNT: "금액",
    METRIC_AVERAGE: "평균 단가",
    METRIC_QUANTITY: "수량",
}


@dataclass(frozen=True, slots=True)
class MetricScope:
    """Which measure family the answer used, and whether the user chose it."""

    family: str
    label: str
    defaulted: bool
    columns: tuple[tuple[str, str], ...] = ()

    @property
    def available_months(self) -> tuple[str, ...]:
        return tuple(period for period, _ in self.columns)


@dataclass(frozen=True, slots=True)
class PeriodScope:
    """The month span an aggregate covered, and how it was decided.

    ``status`` is the contract that keeps §0.2 rule 2 honest:
      ``resolved``     the question named a span and every month of it exists
      ``partial``      the span exists only in part; ``missing`` records the rest
      ``full_span``    the question named no span, so every available month is used
      ``unresolved``   a span was named but cannot be served — the caller must
                       refuse rather than substitute a different span
    """

    status: str
    months: tuple[str, ...] = ()
    request_label: str = ""
    missing: tuple[str, ...] = ()
    reason: str = ""

    @property
    def span_label(self) -> str:
        if not self.months:
            return ""
        if len(self.months) == 1:
            return self.months[0]
        return f"{self.months[0]}~{self.months[-1]}"


def fetch_sql_schema_columns(
    conversation_id: str,
    sources: Sequence[SqlFileSource],
) -> tuple[str, ...]:
    """Return the source column names visible to the file SQL planner."""

    names: list[str] = []
    for source in sources[: _max_schema_tables()]:
        schema = _fetch_schema(source, conversation_id)
        names.extend(
            str(column.get("source_name") or "").strip()
            for column in _schema_columns(schema)
            if str(column.get("source_name") or "").strip()
        )
    return tuple(dict.fromkeys(names))


def query_uploaded_sql(
    question: str,
    conversation_id: str,
    sources: Sequence[SqlFileSource],
) -> SqlQueryOutcome:
    """Plan and execute one read-only query against session-owned file data."""

    if not sources:
        return SqlQueryOutcome("", (), ())
    trace: list[dict[str, str]] = []
    current_stage = "schema"
    try:
        schemas = tuple(
            _fetch_schema(source, conversation_id)
            for source in sources[: _max_schema_tables()]
        )
        trace.append(
            {"stage": "schema", "status": "ok", "table_count": str(len(schemas))}
        )
        if _is_schema_question(question):
            scoped_sources = sources[: len(schemas)]
            data_row_counts = tuple(
                _try_fetch_data_row_count(source, conversation_id)
                for source in scoped_sources
            )
            answer = _render_schema_answer(
                question,
                scoped_sources,
                schemas,
                data_row_counts=data_row_counts,
            )
            return SqlQueryOutcome(
                file_context=answer,
                file_source_items=_source_items(scoped_sources),
                errors=(),
                answer_md=answer,
                trace=tuple(trace),
            )
        if is_ambiguous_file_analysis_question(question):
            answer = _render_file_clarification(schemas)
            trace.append(
                {
                    "stage": "intent",
                    "status": "clarification_needed",
                }
            )
            return SqlQueryOutcome(
                file_context=answer,
                file_source_items=_source_items(sources[: len(schemas)]),
                errors=(),
                answer_md=answer,
                status="clarification_needed",
                trace=tuple(trace),
            )
        current_stage = "measure_validation"
        measure = _measure_request(question, schemas)
        if measure.state == "unsupported":
            answer = file_absence_answer("unsupported", subject=measure.label)
            trace.append(
                {
                    "stage": "measure_validation",
                    "status": "unsupported",
                    "label": measure.label,
                }
            )
            return SqlQueryOutcome(
                file_context="## 업로드 파일 SQL 결과\n상태: 미지원\n" + answer,
                file_source_items=_source_items(sources[: len(schemas)]),
                errors=("file SQL unsupported measure",),
                answer_md=answer,
                status="unsupported_measure",
                trace=tuple(trace),
            )
        current_stage = "period_validation"
        missing_period = _missing_period(question, schemas)
        if missing_period:
            answer = file_absence_answer("missing", period=missing_period)
            trace.append(
                {
                    "stage": "period_validation",
                    "status": "missing",
                    "period": missing_period,
                }
            )
            return SqlQueryOutcome(
                file_context="## 업로드 파일 SQL 결과\n상태: 원천없음\n" + answer,
                file_source_items=_source_items(sources[: len(schemas)]),
                errors=("file SQL missing period",),
                answer_md=answer,
                status="unsupported_period",
                trace=tuple(trace),
            )
        current_stage = "planner"
        resolution = _resolve_deterministic_select(question, schemas)
        plan = resolution.plan
        if plan is None:
            answer = _missing_plan_answer(resolution.missing_slots, question=question)
            trace.append(
                {
                    "stage": "planner",
                    "status": "unsupported",
                    "resolved_slots": ",".join(resolution.resolved_slots),
                    "missing_slots": ",".join(resolution.missing_slots),
                    **_period_trace_fields(resolution.period, resolution.metric),
                }
            )
            return SqlQueryOutcome(
                file_context="## 업로드 파일 SQL 결과\n상태: 미지원\n" + answer,
                file_source_items=_source_items(sources[: len(schemas)]),
                errors=("file SQL deterministic plan unavailable",),
                answer_md=answer,
                status="unsupported_query",
                trace=tuple(trace),
            )
        logical_name = str(plan.get("logical_name") or "").strip()
        sql = str(plan.get("sql") or "").strip()
        source = next(
            (item for item in sources if item.logical_name == logical_name),
            None,
        )
        current_stage = "plan_validation"
        if source is None or not _is_select_only_candidate(sql):
            raise ValueError("planner returned an invalid scoped file query")
        trace.append(
            {
                "stage": "planner",
                "status": "ok",
                "plan_source": "deterministic",
                **_period_trace_fields(resolution.period, resolution.metric),
            }
        )
        aggregate = _is_aggregate_question(question)
        if aggregate and not _has_aggregate_contract(sql):
            return _aggregate_contract_failure(
                trace=(*trace, {"stage": "plan_validation", "status": "contract_failed"})
            )
        schema = next(
            (
                item for item in schemas
                if str(item.get("logical_name") or "").strip() == logical_name
            ),
            {},
        )
        current_stage = "column_validation"
        intent = measure.intent
        if aggregate and intent and not _selected_columns_match_intent(intent, sql, schema):
            return _column_intent_failure(
                intent,
                trace=(*trace, {"stage": "column_validation", "status": "intent_mismatch"}),
            )
        selected_columns = _used_source_columns(sql, schema)
        trace.append(
            {
                "stage": "column_validation",
                "status": "ok",
                "selected_columns": ",".join(selected_columns),
            }
        )
        logger.info(
            "file SQL planner complete plan_source=%s selected_columns=%s",
            "deterministic",
            selected_columns,
        )
        current_stage = "execution"
        result = _run_query(conversation_id, logical_name, sql)
        trace.append({"stage": "execution", "status": "ok"})
        current_stage = "render"
        if _has_no_applied_rows(result):
            answer, filter_label = _no_matching_rows_answer(question, schema)
            trace.append(
                {
                    "stage": "render",
                    "status": "no_matching_rows",
                    "filter": filter_label,
                }
            )
            source_item: dict[str, Any] = {"file_name": source.file_name}
            if source.document_id is not None:
                source_item["document_id"] = source.document_id
            return SqlQueryOutcome(
                file_context="## 업로드 파일 SQL 결과\n상태: 조건 일치 0건\n" + answer,
                file_source_items=(source_item,),
                errors=(),
                answer_md=answer,
                status="no_matching_rows",
                trace=tuple(trace),
            )
        context = _render_result(
            source,
            result,
            schema,
            period=resolution.period,
            metric=resolution.metric,
        )
        answer = ""
        if aggregate:
            answer = _render_aggregate_answer(
                question,
                source,
                sql,
                result,
                schema,
                period=resolution.period,
                metric=resolution.metric,
            )
            if not answer:
                return _aggregate_contract_failure(
                    trace=(*trace, {"stage": "render", "status": "contract_failed"})
                )
        source_item: dict[str, Any] = {"file_name": source.file_name}
        if source.document_id is not None:
            source_item["document_id"] = source.document_id
        return SqlQueryOutcome(
            file_context=context,
            file_source_items=(source_item,),
            errors=(),
            answer_md=answer,
            trace=tuple(trace),
        )
    except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
        logger.exception(
            "file SQL query failed conversation_id=%s logical_names=%s reason=%s",
            conversation_id,
            [source.logical_name for source in sources],
            exc,
        )
        return SqlQueryOutcome(
            file_context=(
                "## 업로드 파일 SQL 결과\n"
                "상태: 확인불가\n"
                "업로드 파일 SQL 질의를 실행하지 못해 요청한 값을 확인할 수 없습니다."
            ),
            file_source_items=(),
            errors=("file SQL query unavailable",),
            answer_md=(
                "업로드 파일 집계 결과를 확인할 수 없습니다. "
                "파일 SQL 조회가 완료되지 않았습니다."
            ),
            status="query_failed",
            trace=tuple(
                [*trace, {"stage": current_stage, "status": "error", "reason": _failure_reason(exc)}]
            ),
        )


def _period_trace_fields(
    period: "PeriodScope | None",
    metric: "MetricScope | None",
) -> dict[str, str]:
    """Expose how period and measure were decided so a wrong span is traceable."""

    fields: dict[str, str] = {}
    if period is not None:
        fields["period_status"] = period.status
        if period.span_label:
            fields["period_span"] = period.span_label
        fields["period_month_count"] = str(len(period.months))
        if period.request_label:
            fields["period_requested"] = period.request_label
        if period.missing:
            fields["period_missing"] = ",".join(period.missing)
        if period.reason:
            fields["period_reason"] = period.reason
    if metric is not None:
        fields["metric_family"] = metric.family
        fields["metric_defaulted"] = "true" if metric.defaulted else "false"
    return fields


def _source_items(sources: Sequence[SqlFileSource]) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    for source in sources:
        item: dict[str, Any] = {"file_name": source.file_name}
        if source.document_id is not None:
            item["document_id"] = source.document_id
        items.append(item)
    return tuple(items)


def _is_schema_question(question: str) -> bool:
    if re.search(
        r"컬럼\s*의.*(?:합계|총계|합산|평균|총액|금액|SUM|AVG)",
        question,
        re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"(?:열\s*목록|컬럼|스키마|헤더|(?:파일|문서|엑셀|시트)\s*구조|"
            r"(?:이|해당)?\s*(?:파일|문서|엑셀)(?:에|에는|은|는)?\s*(?:뭐|무엇|어떤\s*(?:내용|데이터))|"
            r"(?:셀아웃|sell[ -]?out)\s*(?:지표|데이터)?\s*(?:설명|뜻|의미)|"
            r"(?:어떤|무슨)\s*기간\s*(?:데이터|자료)?|기간\s*(?:범위|목록)|"
            r"시트\s*수|행\s*수|마지막\s*(?:월|기간)|월별\s*(?:value|값)\s*열)",
            question,
            re.IGNORECASE,
        )
    )


def _has_period_scoped_measure(question: str) -> bool:
    """Whether the question pins a measure to a span of periods.

    ``2025년 … 매출`` asks for one figure covering twelve months, which is an
    aggregate even though it uses none of the aggregate keywords. Recognising
    this is what lets a span be honoured instead of collapsing to one column.
    """

    if _question_measure_intent(question) is None:
        return False
    return bool(
        month_keys(question)
        or quarter_keys(question)
        or explicit_years(question)
        or relative_span(question) is not None
    )


def _is_aggregate_question(question: str) -> bool:
    return (
        _is_monthly_trend_question(question)
        or _is_growth_by_channel_question(question)
        or _top_n_limit(question) is not None
        or _has_period_scoped_measure(question)
        or _contains_configured_term(
            question,
            "JW_CHAT_FILE_SQL_AGGREGATE_TERMS",
            DEFAULT_AGGREGATE_TERMS,
        )
    )


def _is_monthly_trend_question(question: str) -> bool:
    return bool(
        re.search(
            r"월별\s*(?:추이|흐름|변화|합계|금액|매출|집계)",
            question,
            re.IGNORECASE,
        )
    )


def _is_growth_by_channel_question(question: str) -> bool:
    return bool(
        re.search(r"(?:가장|제일|최대).*(?:성장|증가).*(?:채널)", question)
        or re.search(r"(?:채널).*(?:성장|증가)", question)
    )


def is_ambiguous_file_analysis_question(question: str) -> bool:
    normalized = re.sub(r"[?.!,]+$", "", " ".join(question.split())).strip()
    return normalized in {
        "분석해",
        "분석해줘",
        "분석해주세요",
        "이거 어때",
        "이건 어때",
        "어때",
    }


def _has_aggregate_contract(sql: str) -> bool:
    return bool(re.search(r"\b(?:COUNT|SUM|AVG)\s*\(", sql, re.IGNORECASE)) and bool(
        re.search(r"\bapplied_rows\b", sql, re.IGNORECASE)
    )


def _aggregate_contract_failure(
    *, trace: tuple[dict[str, str], ...] = ()
) -> SqlQueryOutcome:
    answer = (
        "업로드 파일 집계 결과를 확인할 수 없습니다. "
        "필터, 집계 함수, 결과값, 적용 행 수를 모두 검증하지 못했습니다."
    )
    return SqlQueryOutcome(
        file_context="## 업로드 파일 SQL 결과\n상태: 확인불가\n" + answer,
        file_source_items=(),
        errors=("file SQL aggregate contract unavailable",),
        answer_md=answer,
        status="contract_failed",
        trace=trace,
    )


def _column_intent_failure(
    intent: str,
    *,
    trace: tuple[dict[str, str], ...] = (),
) -> SqlQueryOutcome:
    label = {"amount": "금액", "average": "평균", "quantity": "수량", "count": "건수"}.get(
        intent, "요청"
    )
    answer = f"요청하신 {label} 열을 찾지 못했습니다. 열 이름을 지정해 주시겠습니까?"
    return SqlQueryOutcome(
        file_context="## 업로드 파일 SQL 결과\n상태: 확인불가\n" + answer,
        file_source_items=(),
        errors=("file SQL selected column intent mismatch",),
        answer_md=answer,
        status="intent_mismatch",
        trace=trace,
    )


def _render_schema_answer(
    question: str,
    sources: Sequence[SqlFileSource],
    schemas: Sequence[Mapping[str, Any]],
    *,
    data_row_counts: Sequence[int | None],
) -> str:
    lines = ["## 업로드 파일 구조", f"시트 수: {len(schemas)}개"]
    observed_months: list[tuple[int, int, str]] = []
    all_column_names: list[str] = []
    period_source_names: list[str] = []
    for index, schema in enumerate(schemas):
        source = sources[index]
        data_row_count = data_row_counts[index]
        raw_columns = schema.get("columns")
        columns = raw_columns if isinstance(raw_columns, list) else []
        names = [
            str(item.get("source_name") or "").strip()
            for item in columns
            if isinstance(item, dict) and str(item.get("source_name") or "").strip()
        ]
        all_column_names.extend(names)
        lines.extend(
            [
                f"### {source.sheet_name}",
                f"파일: {source.file_name}",
                (
                    f"데이터 행 수: {_format_number(data_row_count)}"
                    if data_row_count is not None
                    else "데이터 행 수: 확인되지 않음"
                ),
                f"열 수: {len(names)}개",
                "열 목록: " + (", ".join(names) if names else "확인되지 않음"),
            ]
        )
        for name in names:
            periods = month_keys(name)
            if periods:
                period_source_names.append(name)
            for period in periods:
                year, month = period.split("-", 1)
                observed_months.append(
                    (int(year), int(month), f"{int(month)}/{year}")
                )
    measure_names = tuple(
        dict.fromkeys(
            name
            for name in all_column_names
            if _is_measure_source_column(name)
        )
    )
    dimension_names = tuple(
        dict.fromkeys(
            name
            for name in all_column_names
            if not _is_measure_source_column(name)
        )
    )
    lines.append(
        "주요 차원 열: "
        + (", ".join(dimension_names) if dimension_names else "확인되지 않음")
    )
    lines.append(
        "측정 열: "
        + (", ".join(measure_names) if measure_names else "확인되지 않음")
    )
    if observed_months:
        earliest = min(observed_months)
        latest = max(observed_months)
        lines.append(
            f"기간 범위: {earliest[0]:04d}-{earliest[1]:02d} ~ "
            f"{latest[0]:04d}-{latest[1]:02d}"
        )
        lines.append(
            "기간 근거 열: " + ", ".join(dict.fromkeys(period_source_names))
        )
        lines.append(f"마지막 월: {latest[2]}")
        next_year, next_month = (latest[0] + 1, 1) if latest[1] == 12 else (latest[0], latest[1] + 1)
        next_label = f"{next_month}/{next_year}"
        present = any(next_label.casefold() in name.casefold() for name in all_column_names)
        lines.append(f"{next_label} 열: {'있음' if present else '없음'}")
    if re.search(r"(?:셀아웃|sell[ -]?out)", question, re.IGNORECASE):
        sellout_names = tuple(
            name
            for name in measure_names
            if _is_amount_column(name)
        )
        lines.append(
            "파일에서 확인된 셀아웃 측정 열: "
            + (", ".join(sellout_names) if sellout_names else "확인되지 않음")
        )
        lines.append(
            "집계 질문에는 질문에 지정한 기간의 실제 열을 선택해 집계합니다."
        )
    for source, data_row_count in zip(
        sources[: len(schemas)],
        data_row_counts,
        strict=True,
    ):
        normalized_sheet = source.sheet_name.casefold()
        if data_row_count is not None and re.search(r"(?:질문|question)", normalized_sheet, re.IGNORECASE):
            lines.append(f"질문 수: {_format_number(data_row_count)}개 (SQL 데이터 실측)")
        if data_row_count is not None and re.search(r"(?:출처|source)", normalized_sheet, re.IGNORECASE):
            lines.append(f"출처 수: {_format_number(data_row_count)}개 (SQL 데이터 실측)")
    return "\n".join(lines)


def _is_measure_source_column(source_name: str) -> bool:
    return bool(month_keys(source_name)) or any(
        predicate(source_name)
        for predicate in (
            _is_amount_column,
            _is_average_column,
            _is_quantity_column,
        )
    )


def _render_file_clarification(
    schemas: Sequence[Mapping[str, Any]],
) -> str:
    column_names = tuple(
        dict.fromkeys(
            str(column.get("source_name") or "").strip()
            for schema in schemas
            for column in _schema_columns(schema)
            if str(column.get("source_name") or "").strip()
        )
    )
    measures = tuple(
        name for name in column_names if _is_measure_source_column(name)
    )
    dimensions = tuple(
        name for name in column_names if not _is_measure_source_column(name)
    )
    manufacturer = next(
        (
            name
            for name in dimensions
            if re.search(
                r"(?:^|\b)(?:mfr|manufacturer|company)(?:\b|$)|제조사|업체",
                name,
                re.IGNORECASE,
            )
        ),
        "",
    )
    product = next(
        (
            name
            for name in dimensions
            if re.search(r"(?:^|\b)product(?:\b|$)|제품", name, re.IGNORECASE)
        ),
        "",
    )
    latest_measure = max(
        measures,
        key=lambda name: max(month_keys(name), default=""),
        default="",
    )
    monthly_measures = tuple(
        name for name in measures if month_keys(name)
    )
    options: list[str] = []
    if manufacturer and latest_measure:
        options.append(f"{manufacturer}별 {latest_measure} 합계")
    if len(monthly_measures) >= 2:
        options.append(f"{latest_measure} 기준 월별 추이")
    if product and latest_measure:
        options.append(f"{product}별 {latest_measure} 상위 10개")
    if not options:
        options.append("파일의 시트·열·기간 구조 보기")
    return "\n".join(
        [
            "어떤 분석을 원하시나요?",
            "현재 파일에서 확인된 열을 기준으로 다음처럼 질문할 수 있습니다.",
            *(f"- {option}" for option in options),
        ]
    )


def _column_list_text(names: Sequence[str], limit: int = 6) -> str:
    """Render source column names on one line.

    Workbook headers can carry embedded newlines (``"VALUES LC SI PRICE\\n1/2026"``),
    which would otherwise split this line and corrupt the surrounding markdown.
    A wide month span is summarised rather than listed in full.
    """

    flattened = [" ".join(str(name).split()) for name in names if str(name).strip()]
    if not flattened:
        return "집계 결과 열"
    if len(flattened) <= limit:
        return ", ".join(flattened)
    return ", ".join(flattened[:limit]) + f" 외 {len(flattened) - limit}개"


def _scope_disclosure_lines(
    period: "PeriodScope | None",
    metric: "MetricScope | None",
    schema: Mapping[str, Any] | None = None,
    *,
    excluded_total_rows: bool = False,
) -> list[str]:
    """State the span and measure an aggregate actually used.

    A reader cannot audit a total whose period and measure are implicit, so both
    are printed even when they were chosen by default — especially then.
    """

    lines: list[str] = []
    if period is not None and period.months:
        span = period.span_label
        count = len(period.months)
        if period.status == "full_span":
            lines.append(f"기간: {span} (파일 전체 {count}개월 · 질문에 기간이 없어 전체를 집계)")
        elif period.status == "partial":
            lines.append(
                f"기간: {span} ({count}개월) — 요청 {period.request_label} 중 "
                f"{len(period.missing)}개월은 파일에 없어 제외"
            )
        else:
            label = f" ({period.request_label})" if period.request_label else ""
            lines.append(f"기간: {span} · {count}개월{label}")
    if metric is not None:
        suffix = " (질문에 지표가 없어 기본값 사용)" if metric.defaulted else ""
        lines.append(f"지표: {_public_metric_label(metric, schema or {})}{suffix}")
    if excluded_total_rows:
        lines.append(
            "집계 제외: 식별 차원이 모두 비어 있는 합계 성격 행 "
            "(파일에는 그대로 남아 있으며 조회 상세에서 확인할 수 있습니다)"
        )
    return lines


def _public_metric_label(metric: "MetricScope", schema: Mapping[str, Any]) -> str:
    source_names = {
        str(item.get("query_name") or ""): str(item.get("source_name") or "")
        for item in _schema_columns(schema)
    }
    names = " ".join(source_names.get(query_name, "") for _, query_name in metric.columns).casefold()
    if "values lc si price" in names:
        return "sell-in 기준 금액"
    if "sell out" in names or "sell-out" in names or "values lc so price" in names:
        return "sell-out 기준 금액"
    if "처방조제" in names or "ubist" in names:
        return "처방조제액"
    return metric.label


def _amount_value_indices(
    columns: Sequence[str],
    applied_index: int,
    metric: "MetricScope | None",
) -> frozenset[int]:
    if metric is None or metric.family != METRIC_AMOUNT:
        return frozenset()
    return frozenset(
        index
        for index, name in enumerate(columns)
        if index != applied_index
        and name.casefold() != "response_count"
        and re.search(r"(?:total|sum|value|amount|sales|growth|period[_-]?\d+|\d{4}[-_/]\d{1,2})", name, re.IGNORECASE)
    )


def _format_aggregate_value(value: Any, *, amount: bool) -> str:
    if not _is_number(value):
        return _markdown_cell(value)
    if amount:
        eok = float(value) / 100_000_000
        if eok and round(eok) == 0:
            return f"{eok:,.2f}".rstrip("0").rstrip(".") + "억원"
        return f"{eok:,.0f}억원"
    return _format_number(value)


def _public_filter_text(where_clause: str, schema: Mapping[str, Any]) -> str:
    """Describe a WHERE clause using the reader's own column names.

    The clause is written against internal query names (``c3``). Printing it
    verbatim exposed them, and the total-row rule added roughly ten more on
    every aggregate. Query names are replaced with the workbook's headers, and
    the total-row clause is dropped because it is already stated in full on its
    own disclosure line.
    """

    text = " ".join(where_clause.split())
    text = _TOTAL_ROW_CLAUSE_RE.sub("", text)
    text = re.sub(r"^\s*(?:AND|OR)\s+|\s+(?:AND|OR)\s*$", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).strip()
    names = {
        str(item.get("query_name") or ""): " ".join(str(item.get("source_name") or "").split())
        for item in _schema_columns(schema)
        if str(item.get("query_name") or "")
    }
    def _swap(match: re.Match[str]) -> str:
        return names.get(match.group(0)) or match.group(0)
    text = re.sub(r"(?<![A-Za-z0-9_])c\d+(?![A-Za-z0-9_])", _swap, text)
    return text or "전체 행"


def _render_aggregate_answer(
    question: str,
    source: SqlFileSource,
    sql: str,
    result: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    period: "PeriodScope | None" = None,
    metric: "MetricScope | None" = None,
) -> str:
    raw_columns = result.get("columns")
    raw_rows = result.get("rows")
    if not isinstance(raw_columns, list) or not isinstance(raw_rows, list) or not raw_rows:
        return ""
    columns = [str(value) for value in raw_columns]
    applied_index = next((index for index, name in enumerate(columns) if name.casefold() == "applied_rows"), None)
    if applied_index is None:
        return ""
    rows = [row for row in raw_rows if isinstance(row, list)]
    if not rows or any(applied_index >= len(row) or not _is_number(row[applied_index]) for row in rows):
        return ""
    where_match = re.search(
        r"\bWHERE\b(.+?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    filter_text = (
        "전체 행"
        if where_match is None
        else _public_filter_text(where_match.group(1), schema)
    )
    aggregate_functions = list(
        dict.fromkeys(
            value.upper()
            for value in re.findall(r"\b(COUNT|SUM|AVG)\s*\(", sql, re.IGNORECASE)
        )
    )
    used_columns = _used_source_columns(sql, schema)
    labels = _source_column_labels(columns, schema)
    amount_indices = _amount_value_indices(columns, applied_index, metric)
    labels = [
        f"{label} (억원)" if index in amount_indices else label
        for index, label in enumerate(labels)
    ]
    total_applied = sum(float(row[applied_index]) for row in rows)
    monthly_periods = _monthly_result_periods(columns, applied_index)
    growth_index = next(
        (
            index
            for index, name in enumerate(columns)
            if name.casefold() == "growth_value"
        ),
        None,
    )
    if (
        _is_growth_by_channel_question(question)
        and len(monthly_periods) >= 2
        and growth_index is not None
    ):
        label_index = next(
            (
                index
                for index in range(len(columns))
                if index not in {applied_index, growth_index}
                and all(index != period_index for _, period_index in monthly_periods)
            ),
            None,
        )
        if label_index is None or any(
            max(label_index, growth_index, applied_index) >= len(row)
            or not _is_number(row[growth_index])
            for row in rows
        ):
            return ""
        first_period, first_index = monthly_periods[0]
        last_period, last_index = monthly_periods[-1]
        if any(
            max(first_index, last_index) >= len(row)
            or not _is_number(row[first_index])
            or not _is_number(row[last_index])
            for row in rows
        ):
            return ""
        lines = [
            "## 업로드 파일 채널 성장 비교",
            f"파일: {source.file_name}",
            f"시트·테이블명: {source.sheet_name} / data",
            f"비교 기준: {first_period} 대비 {last_period} 절대 증가액",
            *_scope_disclosure_lines(period, metric, schema),
            "사용 열: " + (", ".join(used_columns) if used_columns else "집계 결과 열"),
            f"적용 행 수: {_format_number(total_applied)}",
            f"| 채널 | {first_period} (억원) | {last_period} (억원) | 증가액 (억원) | 적용 행 수 |"
            if metric is not None and metric.family == METRIC_AMOUNT
            else f"| 채널 | {first_period} | {last_period} | 증가액 | 적용 행 수 |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            "| "
            + " | ".join(
                (
                    _markdown_cell(row[label_index]),
                    _format_aggregate_value(row[first_index], amount=first_index in amount_indices),
                    _format_aggregate_value(row[last_index], amount=last_index in amount_indices),
                    _format_aggregate_value(row[growth_index], amount=growth_index in amount_indices),
                    _format_number(row[applied_index]),
                )
            )
            + " |"
            for row in rows
        )
        winner = rows[0]
        lines.append(
            f"{first_period} 대비 {last_period} 절대 증가액 기준 "
            f"가장 성장한 채널은 {winner[label_index]}이며 증가액은 "
            f"{_format_aggregate_value(winner[growth_index], amount=growth_index in amount_indices)}입니다."
        )
        return "\n".join(lines)
    if len(rows) == 1 and monthly_periods:
        values = tuple(
            (period, rows[0][index])
            for period, index in monthly_periods
            if index < len(rows[0])
        )
        if not values:
            return ""
        lines = [
            "## 업로드 파일 월별 추이",
            f"파일: {source.file_name}",
            f"시트·테이블명: {source.sheet_name} / data",
            f"필터 조건: {filter_text}",
            *_scope_disclosure_lines(period, metric, schema),
            "사용 열: " + (", ".join(used_columns) if used_columns else "집계 결과 열"),
            "집계 함수: " + ", ".join(aggregate_functions),
            f"적용 행 수: {_format_number(total_applied)}",
            "| 기간 | 합계 (억원) |"
            if metric is not None and metric.family == METRIC_AMOUNT
            else "| 기간 | 합계 |",
            "| --- | --- |",
        ]
        lines.extend(
            f"| {period} | {_format_aggregate_value(value, amount=metric is not None and metric.family == METRIC_AMOUNT)} |"
            for period, value in values
        )
        numeric_values = tuple(
            (period, float(value))
            for period, value in values
            if _is_number(value)
        )
        if len(numeric_values) >= 2:
            first_period, first_value = numeric_values[0]
            last_period, last_value = numeric_values[-1]
            if last_value > first_value:
                direction = "증가했습니다"
            elif last_value < first_value:
                direction = "감소했습니다"
            else:
                direction = "변동이 없었습니다"
            lines.append(
                "월별 흐름: "
                f"{_format_aggregate_value(first_value, amount=metric is not None and metric.family == METRIC_AMOUNT)}에서 "
                f"{_format_aggregate_value(last_value, amount=metric is not None and metric.family == METRIC_AMOUNT)}로 {direction} "
                f"({first_period} → {last_period})."
            )
        return "\n".join(lines)
    lines = [
        "## 업로드 파일 집계 결과",
        f"파일: {source.file_name}",
        f"시트·테이블명: {source.sheet_name} / data",
        f"필터 조건: {filter_text}",
            *_scope_disclosure_lines(
                period,
                metric,
                schema,
                excluded_total_rows=_TOTAL_ROW_EXCLUSION_RE.search(sql) is not None,
            ),
        "사용 열: " + (_column_list_text(used_columns) if used_columns else "집계 결과 열"),
        "집계 함수: " + ", ".join(aggregate_functions),
        f"적용 행 수: {_format_number(total_applied)}",
        "| " + " | ".join(labels) + " |",
        "| " + " | ".join("---" for _ in labels) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _format_aggregate_value(value, amount=index in amount_indices)
                for index, value in enumerate(row[: len(labels)])
            )
            + " |"
        )
    count_index = next(
        (
            index
            for index, name in enumerate(columns)
            if name.casefold() == "response_count"
        ),
        None,
    )
    label_index = next(
        (
            index
            for index in range(len(columns))
            if index not in {applied_index, count_index}
        ),
        None,
    )
    if (
        count_index is not None
        and label_index is not None
        and all(
            max(label_index, count_index) < len(row)
            and _is_number(row[count_index])
            for row in rows
        )
    ):
        dimension_label = labels[label_index]
        summaries = ", ".join(
            f"{dimension_label} {row[label_index]}: {_format_number(row[count_index])}건"
            for row in rows
        )
        lines.append(f"건수 요약: {summaries}입니다.")
    if re.search(r"(?:비교|대비|어느|어디|큰가|더\s*크)", question) and len(rows) >= 2:
        value_index = next(
            (
                index for index, name in enumerate(columns)
                if index != applied_index
                and re.search(
                    r"(?:total|sum|avg|value|amount|sales|금액|합계)",
                    name,
                    re.IGNORECASE,
                )
            ),
            None,
        )
        label_index = next((index for index in range(len(columns)) if index not in {applied_index, value_index}), None)
        if (
            value_index is not None
            and label_index is not None
            and all(
                value_index < len(row) and _is_number(row[value_index])
                for row in rows[:2]
            )
        ):
            left, right = rows[0], rows[1]
            left_value, right_value = float(left[value_index]), float(right[value_index])
            winner = left if left_value >= right_value else right
            lines.append(
                f"비교 결론: {winner[label_index]}이(가) "
                f"{_format_aggregate_value(abs(left_value - right_value), amount=value_index in amount_indices)}만큼 더 큽니다."
            )
    return "\n".join(lines)


def _used_source_columns(sql: str, schema: Mapping[str, Any]) -> list[str]:
    raw_columns = schema.get("columns")
    columns = raw_columns if isinstance(raw_columns, list) else []
    return [
        str(item.get("source_name") or "")
        for item in columns
        if isinstance(item, dict)
        and str(item.get("query_name") or "")
        and re.search(rf"(?<![A-Za-z0-9_]){re.escape(str(item.get('query_name')))}(?![A-Za-z0-9_])", sql)
    ]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _format_number(value: int | float | None) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.6f}".rstrip("0").rstrip(".")


def _fetch_schema(source: SqlFileSource, conversation_id: str) -> dict[str, Any]:
    response = requests.post(
        f"{_file_service_base_url()}/file-sql/schema",
        json=_session_payload(
            conversation_id,
            logical_name=source.logical_name,
        ),
        headers=code_serving_actor_headers(),
        timeout=_file_service_timeout(),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("file SQL schema response must be an object")
    return {
        **body,
        "file_name": source.file_name,
        "sheet_name": source.sheet_name,
    }


def _deterministic_select(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """Plan only file-query shapes whose slots are fully grounded in schema and text."""

    return _resolve_deterministic_select(question, schemas).plan


def _resolve_deterministic_select(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
) -> DeterministicPlanResolution:
    """Resolve file-query slots independently and never invent an ungrounded plan."""

    best_failure = DeterministicPlanResolution(None, (), ("요청한 조건",))
    requested_sheet = _requested_sheet_name(question, schemas)
    scoped_schemas = tuple(schemas)
    if requested_sheet:
        scoped_schemas = tuple(
            schema
            for schema in schemas
            if str(schema.get("sheet_name") or "").strip().casefold()
            == requested_sheet.casefold()
        )
        if not scoped_schemas:
            available = ", ".join(
                dict.fromkeys(
                    str(schema.get("sheet_name") or "").strip()
                    for schema in schemas
                    if str(schema.get("sheet_name") or "").strip()
                )
            )
            suffix = f" (사용 가능: {available})" if available else ""
            return DeterministicPlanResolution(
                None,
                (),
                (f"시트 '{requested_sheet}'{suffix}",),
            )

    for schema in scoped_schemas:
        columns = _schema_columns(schema)
        by_source = {
            str(item.get("source_name") or "").strip().casefold(): str(item.get("query_name") or "").strip()
            for item in columns
        }
        if "q1" in question.casefold() and re.search(r"(?<![a-z])no(?![a-z])", question, re.IGNORECASE):
            q1_column = by_source.get("q1")
            no_column = by_source.get("no")
            if q1_column and no_column:
                value_text = re.sub(r"\bq1\b", "", question, flags=re.IGNORECASE)
                values = tuple(
                    dict.fromkeys(re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", value_text))
                )
                where = ""
                if values:
                    where = " WHERE " + q1_column + " IN (" + ", ".join(_sql_literal(value) for value in values) + ")"
                return DeterministicPlanResolution(
                    {
                        "logical_name": str(schema.get("logical_name") or ""),
                        "sql": (
                            f"SELECT {q1_column}, COUNT(*) AS response_count, "
                            f"SUM({no_column}) AS no_total, COUNT(*) AS applied_rows "
                            f"FROM data{where} GROUP BY {q1_column} ORDER BY {q1_column}"
                        ),
                    },
                    ("q1", "no"),
                )

        if not _is_aggregate_question(question):
            continue

        if _is_unscoped_bare_aggregate(question):
            return DeterministicPlanResolution(None, (), ("집계 대상",))

        resolved: list[str] = []
        missing: list[str] = []
        if _is_growth_by_channel_question(question):
            channel = _find_column(columns, r"(?:^|\b)channel(?:\b|$)|채널")
            monthly_measures = _monthly_measure_columns(columns)
            if channel is None:
                missing.append("채널")
            if len(monthly_measures) < 2:
                missing.append("비교 가능한 월별 금액 열")
            if missing:
                candidate = DeterministicPlanResolution(
                    None,
                    (),
                    tuple(missing),
                )
                if len(candidate.resolved_slots) >= len(best_failure.resolved_slots):
                    best_failure = candidate
                continue
            first_period, first_query = monthly_measures[0]
            last_period, last_query = monthly_measures[-1]
            channel_query = str(channel.get("query_name") or "")
            return DeterministicPlanResolution(
                {
                    "logical_name": str(schema.get("logical_name") or ""),
                    "sql": (
                        f"SELECT {channel_query}, "
                        f"SUM({first_query}) AS period_{first_period.replace('-', '_')}, "
                        f"SUM({last_query}) AS period_{last_period.replace('-', '_')}, "
                        f"(SUM({last_query}) - SUM({first_query})) AS growth_value, "
                        f"COUNT(*) AS applied_rows FROM data "
                        f"WHERE {channel_query} IS NOT NULL AND "
                        f"TRIM({channel_query}) <> '' "
                        f"GROUP BY {channel_query} ORDER BY growth_value DESC"
                    ),
                },
                ("channel", "growth_periods"),
            )
        manufacturer = _find_column(columns, r"(?:^|\b)(?:mfr|manufacturer|company)(?:\b|$)|제조사|업체")
        intent = _question_measure_intent(question) or "amount"
        monthly_trend = _is_monthly_trend_question(question)
        monthly_measures = (
            _monthly_measure_columns(columns)
            if monthly_trend
            else ()
        )
        measure = (
            None
            if intent == "count" or monthly_measures
            else _find_measure_column(columns, question)
        )
        metric_scope = (
            None
            if intent == "count" or monthly_measures
            else _resolve_metric_scope(question, columns)
        )
        period_scope = (
            _resolve_period_scope(question, metric_scope.available_months)
            if metric_scope is not None
            else None
        )
        if monthly_trend and not monthly_measures:
            missing.append("월별 금액 열")
            aggregate_expression = ""
            aggregate_alias = "total_value"
        elif monthly_measures:
            aggregate_expression = ", ".join(
                f"SUM({query_name}) AS period_{period.replace('-', '_')}"
                for period, query_name in monthly_measures
            )
            aggregate_alias = f"period_{monthly_measures[-1][0].replace('-', '_')}"
            resolved.append("monthly_measures")
        elif intent == "count":
            aggregate_expression = "COUNT(*) AS response_count"
            aggregate_alias = "response_count"
            resolved.append("measure")
        elif period_scope is not None and period_scope.status == "unresolved":
            # The question named a span this workbook cannot serve. Refusing is
            # the contract: substituting a different span would answer a
            # question nobody asked (§0.2 rule 2).
            missing.append(
                f"요청 기간({period_scope.request_label})"
                if period_scope.request_label
                else "요청 기간"
            )
            aggregate_expression = ""
            aggregate_alias = "total_value"
        elif metric_scope is not None and period_scope is not None:
            if intent == "average":
                aggregate_expression = ""
                if len(period_scope.months) == 1:
                    single = _period_sum_expression(metric_scope, period_scope.months)
                    aggregate_expression = single.replace("SUM(", "AVG(", 1)
            else:
                aggregate_expression = _period_sum_expression(
                    metric_scope, period_scope.months
                )
            aggregate_alias = "total_value"
            if aggregate_expression:
                resolved.extend(("measure", "period"))
            else:
                missing.append(_measure_label(intent))
        elif measure is not None:
            measure_query = str(measure.get("query_name") or "")
            function = "AVG" if intent == "average" else "SUM"
            aggregate_expression = f"{function}({measure_query}) AS total_value"
            aggregate_alias = "total_value"
            resolved.append("measure")
        else:
            requested = _requested_measure_label(question)
            missing.append(requested or _measure_label(intent))
            aggregate_expression = ""
            aggregate_alias = "total_value"

        filters: list[str] = []
        select_prefix = ""
        group_suffix = ""
        subjects = _file_comparison_subjects(question)
        if _file_query_intent(question) == "file_compare":
            if manufacturer is None:
                missing.append("제조사")
            elif len(subjects) != 2:
                missing.append("비교 대상")
            else:
                manufacturer_query = str(manufacturer.get("query_name") or "")
                filters.append(
                    f"{manufacturer_query} IN ({', '.join(_sql_literal(value) for value in subjects)})"
                )
                select_prefix = f"{manufacturer_query}, "
                group_suffix = (
                    f" GROUP BY {manufacturer_query} ORDER BY {aggregate_alias} DESC"
                )
                resolved.extend(("manufacturer", "subjects"))
        else:
            if re.search(
                r"제조사\s*별|업체\s*별|by\s+(?:mfr|manufacturer|company)",
                question,
                re.IGNORECASE,
            ):
                if manufacturer is None:
                    missing.append("제조사")
                else:
                    manufacturer_query = str(manufacturer.get("query_name") or "")
                    select_prefix = f"{manufacturer_query}, "
                    group_suffix = (
                        f" GROUP BY {manufacturer_query} ORDER BY {aggregate_alias} DESC"
                    )
                    resolved.append("manufacturer")
            elif re.search(
                r"채널\s*별|by\s+channel",
                question,
                re.IGNORECASE,
            ):
                channel = _find_column(columns, r"(?:^|\b)channel(?:\b|$)|채널")
                if channel is None:
                    missing.append("채널")
                else:
                    channel_query = str(channel.get("query_name") or "")
                    select_prefix = f"{channel_query}, "
                    group_suffix = (
                        f" GROUP BY {channel_query} ORDER BY {aggregate_alias} DESC"
                    )
                    resolved.append("channel")
            elif re.search(r"제품\s*별|product(?:\s+name)?", question, re.IGNORECASE) or (
                _top_n_limit(question) is not None
                and re.search(r"제품(?:명)?", question, re.IGNORECASE)
            ):
                product = _find_column(
                    columns,
                    r"(?:^|\b)product(?:\s+name)?(?:\b|$)|제품(?:명)?",
                )
                if product is None:
                    missing.append("제품")
                else:
                    product_query = str(product.get("query_name") or "")
                    filters.extend(
                        (
                            f"{product_query} IS NOT NULL",
                            f"TRIM({product_query}) <> ''",
                        )
                    )
                    select_prefix = f"{product_query}, "
                    group_suffix = (
                        f" GROUP BY {product_query} ORDER BY {aggregate_alias} DESC"
                    )
                    if top_n := _top_n_limit(question):
                        group_suffix += f" LIMIT {top_n}"
                    resolved.append("product")
            subject = _single_manufacturer_subject(question)
            if subject:
                if manufacturer is None:
                    missing.append("제조사")
                else:
                    manufacturer_query = str(manufacturer.get("query_name") or "")
                    filters.append(f"{manufacturer_query} = {_sql_literal(subject)}")
                    resolved.append("manufacturer")
            elif product_subject := _product_subject(question, columns):
                product = _find_column(
                    columns,
                    r"(?:^|\b)product(?:\s+name)?(?:\b|$)|제품(?:명)?",
                )
                product_query = str(product.get("query_name") or "") if product else ""
                if product_query:
                    # Project the dimension as well as filtering on it, so the
                    # answer shows which value was matched rather than asking
                    # the reader to trust the filter.
                    filters.append(f"{product_query} = {_sql_literal(product_subject)}")
                    select_prefix = f"{product_query}, "
                    group_suffix = f" GROUP BY {product_query}"
                    resolved.append("product_subject")

        atc_code = _atc4_code(question)
        if atc_code:
            atc_column = _find_column(columns, r"atc\s*4")
            if atc_column is None:
                missing.append("ATC4")
            else:
                atc_query = str(atc_column.get("query_name") or "")
                atc_prefix = _sql_literal(atc_code + "\\_%")
                filters.append(
                    f"({atc_query} = {_sql_literal(atc_code)} OR "
                    f"{atc_query} LIKE {atc_prefix} ESCAPE '\\')"
                )
                resolved.append("ATC4")

        if missing:
            candidate = DeterministicPlanResolution(
                None,
                tuple(dict.fromkeys(resolved)),
                tuple(dict.fromkeys(missing)),
                period=period_scope,
                metric=metric_scope,
            )
            if len(candidate.resolved_slots) >= len(best_failure.resolved_slots):
                best_failure = candidate
            continue

        exclusion = _aggregate_row_exclusion(columns)
        if exclusion:
            filters.append(exclusion)
            resolved.append("total_row_exclusion")
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        return DeterministicPlanResolution(
            {
                "logical_name": str(schema.get("logical_name") or ""),
                "sql": (
                    f"SELECT {select_prefix}{aggregate_expression}, COUNT(*) AS applied_rows "
                    f"FROM data{where}{group_suffix}"
                ),
            },
            tuple(dict.fromkeys(resolved)),
            period=period_scope,
            metric=metric_scope,
        )
    return best_failure


def _top_n_limit(question: str) -> int | None:
    match = re.search(
        r"(?:상위\s*(\d+)\s*(?:개|건)?|top\s*(\d+))",
        question,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return min(max(int(match.group(1) or match.group(2)), 1), 100)


def _measure_label(intent: str) -> str:
    return {
        "amount": "금액",
        "average": "평균",
        "quantity": "수량",
        "count": "건수",
    }.get(intent, "요청한 지표")


def _missing_plan_answer(
    missing_slots: Sequence[str],
    *,
    question: str = "",
) -> str:
    labels = tuple(dict.fromkeys(value for value in missing_slots if value))
    if labels == ("집계 대상",):
        return "무엇의 합계인지 명확하지 않습니다. 제조사 또는 측정 항목을 지정해 주세요."
    normalized_question = question.casefold()
    if labels == ("요청한 조건",) and (
        "셀아웃" in normalized_question
        or "sell-out" in normalized_question
        or "sell out" in normalized_question
    ):
        return (
            "셀아웃 데이터는 확인했습니다. 어떤 기준으로 분석할까요? "
            "예를 들어 2026년 1월 총 sell-out 금액, 제조사별 합계, "
            "제품별 상위 10개처럼 질문해 주세요."
        )
    if len(labels) == 1 and labels[0] != "요청한 조건":
        return file_absence_answer("unsupported", subject=labels[0])
    detail = ", ".join(labels) if labels else "요청한 조건"
    return f"이 파일에서 {detail}을 찾을 수 없습니다. 파일의 열 이름을 확인해 주세요."


def _schema_columns(schema: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = schema.get("columns")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _find_column(
    columns: Sequence[Mapping[str, Any]], pattern: str
) -> Mapping[str, Any] | None:
    return next(
        (
            item
            for item in columns
            if re.search(pattern, str(item.get("source_name") or ""), re.IGNORECASE)
        ),
        None,
    )


def _find_measure_column(
    columns: Sequence[Mapping[str, Any]], question: str
) -> Mapping[str, Any] | None:
    requested_months = month_keys(question)
    candidates = tuple(
        item
        for item in columns
        if _is_amount_column(str(item.get("source_name") or ""))
        and not _is_average_column(str(item.get("source_name") or ""))
        and not _is_quantity_column(str(item.get("source_name") or ""))
    )
    if requested_months:
        return next(
            (
                item
                for item in candidates
                if requested_months.intersection(month_keys(str(item.get("source_name") or "")))
            ),
            None,
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: max(
            month_keys(str(item.get("source_name") or "")), default=""
        ),
    )


# Headers that describe a row rather than identify one. A workbook-level total
# row still carries these (``Grand Total``), so they must not count as identity.
_DESCRIPTOR_COLUMN_RE: Final[re.Pattern[str]] = re.compile(
    r"audit|desc|구분|유형|비고|note|remark", re.IGNORECASE
)
_MAX_IDENTITY_PREDICATE_COLUMNS: Final = 12
# Recognises the predicate emitted by ``_aggregate_row_exclusion`` so the
# renderer can disclose the exclusion without re-deriving it.
_TOTAL_ROW_EXCLUSION_RE: Final[re.Pattern[str]] = re.compile(
    r"COALESCE\(TRIM\(c\d+\), ''\) <> ''", re.IGNORECASE
)
# The whole parenthesised total-row rule, so it can be lifted out of the
# human-facing filter description instead of being spelled out column by column.
_TOTAL_ROW_CLAUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"\(\s*COALESCE\(TRIM\(c\d+\), ''\) <> ''"
    r"(?:\s+OR\s+COALESCE\(TRIM\(c\d+\), ''\) <> '')*\s*\)",
    re.IGNORECASE,
)


def _identity_dimension_columns(
    columns: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return query names of columns that identify which entity a row is about.

    Period-measure columns are excluded because they hold values, and descriptor
    columns are excluded because a total row fills them in too.
    """

    identity: list[str] = []
    for item in columns:
        source_name = str(item.get("source_name") or "")
        query_name = str(item.get("query_name") or "")
        if not query_name or month_keys(source_name):
            continue
        if _DESCRIPTOR_COLUMN_RE.search(source_name):
            continue
        identity.append(query_name)
    return tuple(identity)


def _aggregate_row_exclusion(columns: Sequence[Mapping[str, Any]]) -> str:
    """Build a predicate that keeps only rows identifying a real entity.

    Exported workbooks frequently carry a trailing ``Grand Total`` row whose
    identity columns are all blank. Summing it alongside its own components
    doubles every unfiltered total, so it is excluded from aggregation while the
    row itself stays in the table and remains retrievable (§0.2 rule 2).

    The rule is structural, not a match on the words ``Grand Total``: a row is
    excluded only when *every* identity dimension is blank. Anything ambiguous —
    even one populated dimension — is kept.
    """

    identity = _identity_dimension_columns(columns)[:_MAX_IDENTITY_PREDICATE_COLUMNS]
    if not identity:
        return ""
    terms = " OR ".join(f"COALESCE(TRIM({name}), '') <> ''" for name in identity)
    return f"({terms})"


def _metric_family(source_name: str) -> str | None:
    """Classify one period column into a measure family, or ``None`` if unclear."""

    if _is_average_column(source_name):
        return METRIC_AVERAGE
    if _is_quantity_column(source_name) or _UNIT_COLUMN_RE.search(source_name):
        return METRIC_QUANTITY
    if _is_amount_column(source_name):
        return METRIC_AMOUNT
    return None


def _metric_period_columns(
    columns: Sequence[Mapping[str, Any]],
    family: str,
) -> tuple[tuple[str, str], ...]:
    """Return ascending ``(period, query_name)`` pairs for one measure family."""

    pairs: list[tuple[str, str]] = []
    for item in columns:
        source_name = str(item.get("source_name") or "")
        query_name = str(item.get("query_name") or "")
        if not query_name or _metric_family(source_name) != family:
            continue
        pairs.extend((period, query_name) for period in month_keys(source_name))
    return tuple(sorted(dict.fromkeys(pairs)))


def _resolve_metric_scope(
    question: str,
    columns: Sequence[Mapping[str, Any]],
) -> MetricScope | None:
    """Pick the measure family the question asks for, defaulting to amount.

    A default is still a choice the reader must be able to see, so the returned
    scope records ``defaulted`` and the renderer states it.
    """

    intent = _question_measure_intent(question)
    requested = {
        "average": METRIC_AVERAGE,
        "quantity": METRIC_QUANTITY,
        "amount": METRIC_AMOUNT,
    }.get(intent or "")
    if requested is not None:
        # An explicit measure is never substituted. Answering an amount question
        # from an average-price column would be the same silent swap this round
        # exists to remove, so an absent family yields no plan at all.
        period_columns = _metric_period_columns(columns, requested)
        if not period_columns:
            return None
        return MetricScope(
            family=requested,
            label=_METRIC_LABELS.get(requested, requested),
            defaulted=False,
            columns=period_columns,
        )
    for family in (METRIC_AMOUNT, METRIC_QUANTITY, METRIC_AVERAGE):
        period_columns = _metric_period_columns(columns, family)
        if period_columns:
            return MetricScope(
                family=family,
                label=_METRIC_LABELS.get(family, family),
                defaulted=True,
                columns=period_columns,
            )
    return None


def _resolve_period_scope(question: str, available: Sequence[str]) -> PeriodScope:
    """Resolve the month span a question asks for against the months that exist.

    Never widens or substitutes: if the question names a span that the workbook
    cannot serve, the result is ``unresolved`` and the caller must refuse.
    """

    months = tuple(dict.fromkeys(available))
    if not months:
        return PeriodScope(status="unresolved", reason="no_period_columns")

    requested: list[str] = []
    labels: list[str] = []

    explicit_months = month_keys(question)
    if explicit_months:
        requested.extend(sorted(explicit_months))
        labels.extend(sorted(explicit_months))
    for quarter in sorted(quarter_keys(question)):
        requested.extend(quarter_months(quarter))
        labels.append(quarter)
    for year in explicit_years(question):
        requested.extend(year_months(year))
        labels.append(f"{year}년")

    span = relative_span(question)
    if not requested and span is not None:
        count, unit = span
        window = count * 12 if unit == "년" else count
        requested.extend(months_back(months[-1], window))
        labels.append(f"최근 {count}{unit}")

    if not requested:
        return PeriodScope(status="full_span", months=months, request_label="")

    request_label = " · ".join(dict.fromkeys(labels))
    wanted = tuple(dict.fromkeys(requested))
    covered = tuple(month for month in months if month in set(wanted))
    missing = tuple(month for month in wanted if month not in set(months))
    if not covered:
        return PeriodScope(
            status="unresolved",
            request_label=request_label,
            missing=missing,
            reason="requested_period_absent",
        )
    if missing:
        return PeriodScope(
            status="partial",
            months=covered,
            request_label=request_label,
            missing=missing,
            reason="requested_period_partially_absent",
        )
    return PeriodScope(status="resolved", months=covered, request_label=request_label)


def _period_sum_expression(
    metric: MetricScope,
    period_months: Sequence[str],
    alias: str = "total_value",
) -> str:
    """Sum one measure family across a month span.

    ``COALESCE(SUM(col), 0)`` per column keeps two contracts at once: a column
    that is entirely NULL contributes zero instead of nulling the whole sum, and
    every term stays a bare ``SUM(column)`` so the existing selected-column
    intent check can still read what was aggregated.
    """

    wanted = set(period_months)
    query_names = tuple(
        dict.fromkeys(
            query_name for period, query_name in metric.columns if period in wanted
        )
    )
    if not query_names:
        return ""
    if len(query_names) == 1:
        return f"SUM({query_names[0]}) AS {alias}"
    terms = " + ".join(f"COALESCE(SUM({name}), 0)" for name in query_names)
    return f"{terms} AS {alias}"


def _monthly_measure_columns(
    columns: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    monthly: list[tuple[str, str]] = []
    for item in columns:
        source_name = str(item.get("source_name") or "")
        query_name = str(item.get("query_name") or "")
        periods = month_keys(source_name)
        if (
            query_name
            and periods
            and _is_amount_column(source_name)
            and not _is_average_column(source_name)
            and not _is_quantity_column(source_name)
        ):
            monthly.extend((period, query_name) for period in periods)
    return tuple(sorted(dict.fromkeys(monthly)))


def _monthly_result_periods(
    columns: Sequence[str],
    applied_index: int,
) -> tuple[tuple[str, int], ...]:
    periods: list[tuple[str, int]] = []
    for index, column in enumerate(columns):
        if index == applied_index:
            continue
        match = re.fullmatch(r"period_(20\d{2})_(0[1-9]|1[0-2])", column)
        if match is not None:
            periods.append((f"{match.group(1)}-{match.group(2)}", index))
    return tuple(sorted(periods))


def _file_comparison_subjects(question: str) -> tuple[str, ...]:
    match = re.search(
        r"([가-힣A-Za-z0-9_-]+)(?:와|과)\s*([가-힣A-Za-z0-9_-]+)(?:의|\s+비교|\s+대비|$)",
        question,
    )
    if match is None:
        return ()
    return tuple(value.strip() for value in match.groups())


def _single_manufacturer_subject(question: str) -> str:
    matches = re.finditer(
        r"([가-힣A-Za-z0-9_-]+(?:제약|약품))(?:의|은|는|이|가|에서)?\s*"
        r"(?:월별\s*)?(?:sell[ -]?out|매출|금액|합계|총액)",
        question,
        re.IGNORECASE,
    )
    for match in matches:
        candidate = match.group(1).strip()
        if not re.fullmatch(r"[A-Z]\d{2}[A-Z]\d", candidate, re.IGNORECASE):
            return candidate
    return ""


_SUBJECT_MEASURE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s,])(?P<subject>[가-힣A-Za-z0-9][가-힣A-Za-z0-9._-]{1,})"
    r"(?:의|은|는|이|가)?\s*"
    r"(?:\d{2,4}\s*년\s*)?(?:\d{1,2}\s*(?:월|분기)\s*)?(?:최근\s*\d{1,2}\s*(?:년|개월|달)\s*)?"
    r"(?:매출|판매액|판매|금액|총액|합계|sell[ -]?out)",
    re.IGNORECASE,
)
# Words that look like a subject but name a grouping axis, a measure, or the
# workbook itself. A subject drawn from this set would filter on a value that
# does not exist and quietly return nothing.
_SUBJECT_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "매출", "판매", "판매액", "금액", "총액", "합계", "총계", "합산", "집계", "평균",
        "수량", "단가", "건수", "개수", "제품", "제품명", "브랜드", "제조사", "업체",
        "회사", "채널", "카테고리", "분류", "시장", "전체", "상위", "하위", "순위",
        "파일", "문서", "엑셀", "시트", "데이터", "기준", "그리고", "각각", "비교",
        "대비", "알려줘", "보여줘", "확인", "정리", "요약",
    }
)


def _product_subject(question: str, columns: Sequence[Mapping[str, Any]]) -> str:
    """Return a single product value the question scopes its measure to.

    Only fires for questions that name one subject next to a measure. Grouping
    questions (``제품별``, ``상위 N``) already project the dimension, so adding a
    filter there would narrow an answer the user asked to be broad.

    A subject that does not exist in the workbook yields zero rows, which the
    caller reports as ``조건 일치 0건`` — never as a number from a wider scope.
    """

    if _top_n_limit(question) is not None:
        return ""
    if re.search(r"[가-힣A-Za-z]{2,}\s*별", question):
        return ""
    if _find_column(columns, r"(?:^|\b)product(?:\s+name)?(?:\b|$)|제품(?:명)?") is None:
        return ""
    candidates: list[str] = []
    for match in _SUBJECT_MEASURE_RE.finditer(question):
        subject = match.group("subject").strip()
        if not subject or subject in _SUBJECT_STOPWORDS:
            continue
        if subject.endswith("별"):
            continue
        if re.fullmatch(r"[0-9]+(?:년|월|분기|개월|개|건)?", subject):
            continue
        if month_keys(subject) or quarter_keys(subject) or explicit_years(subject):
            continue
        if re.fullmatch(r"[A-Z]\d{2}[A-Z]\d", subject, re.IGNORECASE):
            continue
        if subject not in candidates:
            candidates.append(subject)
    return candidates[0] if len(candidates) == 1 else ""


def _requested_sheet_name(
    question: str,
    schemas: Sequence[Mapping[str, Any]] = (),
) -> str:
    known_names = sorted(
        {
            str(schema.get("sheet_name") or "").strip()
            for schema in schemas
            if str(schema.get("sheet_name") or "").strip()
        },
        key=len,
        reverse=True,
    )
    for name in known_names:
        if re.search(rf"(?<![0-9A-Za-z가-힣]){re.escape(name)}\s*시트", question, re.IGNORECASE):
            return name
    match = re.search(
        r"(?<![0-9A-Za-z가-힣_.-])([0-9A-Za-z가-힣_.-]+)\s*시트(?:에서|의|는|은|이|가)?",
        question,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _is_unscoped_bare_aggregate(question: str) -> bool:
    normalized = " ".join(question.split())
    if _question_measure_intent(normalized) or _requested_measure_label(normalized):
        return False
    if month_keys(normalized) or _single_manufacturer_subject(normalized):
        return False
    if _file_query_intent(normalized) or _atc4_code(normalized) or _requested_sheet_name(normalized):
        return False
    return bool(re.fullmatch(r"(?:합계|총계|합산|평균|총액|집계)(?:는|은|이|가)?[?.,!]?", normalized))


def _file_query_intent(question: str) -> str | None:
    if re.search(r"(?:비교|대비|각각)", question) and len(
        _file_comparison_subjects(question)
    ) == 2:
        return "file_compare"
    return None


def _atc4_code(question: str) -> str:
    match = re.search(
        r"\bATC\s*4?\s*[:=]?\s*([A-Z][A-Z0-9]{3,6})(?=[^A-Z0-9]|$)",
        question,
        re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r"(?<![A-Z0-9])([A-Z]\d{2}[A-Z]\d)(?![A-Z0-9])",
            question,
            re.IGNORECASE,
        )
    return match.group(1).upper() if match else ""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _used_source_columns(sql: str, schema: Mapping[str, Any]) -> tuple[str, ...]:
    matches: list[tuple[int, str]] = []
    for item in _schema_columns(schema):
        query_name = str(item.get("query_name") or "")
        if not query_name:
            continue
        match = re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(query_name)}(?![A-Za-z0-9_])",
            sql,
        )
        if match is not None:
            matches.append((match.start(), str(item.get("source_name") or "")))
    return tuple(source_name for _, source_name in sorted(matches))


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, requests.RequestException):
        return "request_error"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "validation_error"
    return "execution_error"


def _generate_select(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """Retain the legacy test seam while structurally disabling LLM SQL generation."""

    del question, schemas
    raise RuntimeError("LLM file SQL generation is disabled")


def _run_query(
    conversation_id: str,
    logical_name: str,
    sql: str,
) -> dict[str, Any]:
    response = requests.post(
        f"{_file_service_base_url()}/file-sql/query",
        json=_session_payload(
            conversation_id,
            logical_name=logical_name,
            sql=sql,
        ),
        headers=code_serving_actor_headers(),
        timeout=_file_service_timeout(),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("file SQL query response must be an object")
    return body


def _fetch_data_row_count(source: SqlFileSource, conversation_id: str) -> int:
    result = _run_query(
        conversation_id,
        source.logical_name,
        "SELECT COUNT(*) AS data_row_count FROM data",
    )
    columns = result.get("columns")
    rows = result.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or not rows:
        raise ValueError("file SQL row count response is empty")
    try:
        index = columns.index("data_row_count")
        value = rows[0][index]
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError("file SQL row count response is malformed") from exc
    if not _is_number(value) or float(value) < 0 or not float(value).is_integer():
        raise ValueError("file SQL row count must be a non-negative integer")
    return int(value)


def _try_fetch_data_row_count(
    source: SqlFileSource,
    conversation_id: str,
) -> int | None:
    try:
        return _fetch_data_row_count(source, conversation_id)
    except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
        logger.warning(
            "file SQL data row count unavailable logical_name=%s reason=%s",
            source.logical_name,
            _failure_reason(exc),
        )
        return None


def _compact_schema(question: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    raw_columns = schema.get("columns")
    columns = [item for item in raw_columns if isinstance(item, dict)] if isinstance(raw_columns, list) else []
    total_column_count = len(columns)
    cap = _max_schema_columns()
    if total_column_count > cap:
        tokens = _question_tokens(question)
        question_months = month_keys(question)
        matched = [
            item
            for item in columns
            if _column_matches_question(item, tokens, question_months)
        ]
        identity = columns[: min(_identity_column_count(), cap)]
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in [*identity, *matched]:
            query_name = str(item.get("query_name") or "")
            if query_name and query_name not in seen:
                seen.add(query_name)
                selected.append(item)
            if len(selected) >= cap:
                break
        columns = selected
    omitted_column_count = max(total_column_count - len(columns), 0)
    return {
        "logical_name": str(schema.get("logical_name") or ""),
        "file_name": str(schema.get("file_name") or ""),
        "sheet_name": str(schema.get("sheet_name") or ""),
        "query_table": "data",
        "columns": columns,
        "schema_truncated": omitted_column_count > 0,
        "total_column_count": total_column_count,
        "omitted_column_count": omitted_column_count,
        "selection_notice": (
            "Schema was compacted; related columns may be omitted. "
            "Return an empty plan rather than guessing when the requested measure is absent."
            if omitted_column_count
            else ""
        ),
    }


def _render_result(
    source: SqlFileSource,
    result: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    period: "PeriodScope | None" = None,
    metric: "MetricScope | None" = None,
) -> str:
    """Render the rows a file query returned, with the span they cover.

    This is the block the document lane publishes as evidence, so the span and
    measure have to be stated here too. Putting them only in the aggregate
    answer left a market-scope reader a total with no period attached.
    """

    columns = result.get("columns")
    rows = result.get("rows")
    safe_columns = _source_column_labels(columns, schema)
    safe_rows = rows if isinstance(rows, list) else []
    lines = [
        "## 업로드 파일 SQL 결과",
        f"파일: {source.file_name}",
        f"시트: {source.sheet_name}",
        *_scope_disclosure_lines(period, metric, schema),
    ]
    if not safe_rows:
        return "\n".join([*lines, "상태: 원천없음", "원천 조회 결과 0행"])
    if not safe_columns:
        raise ValueError("file SQL query returned rows without columns")
    lines.extend(
        [
            "상태: 확인됨",
            "| " + " | ".join(_markdown_cell(value) for value in safe_columns) + " |",
            "| " + " | ".join("---" for _ in safe_columns) + " |",
        ]
    )
    for row in safe_rows:
        if not isinstance(row, list):
            raise ValueError("file SQL row must be a list")
        values = [*row[: len(safe_columns)], *("" for _ in range(max(0, len(safe_columns) - len(row))))]
        lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    return "\n".join(lines)


def _has_no_applied_rows(result: Mapping[str, Any]) -> bool:
    columns = result.get("columns")
    rows = result.get("rows")
    if not isinstance(rows, list):
        return False
    if not rows:
        return True
    if not isinstance(columns, list):
        return False
    applied_index = next(
        (
            index
            for index, name in enumerate(columns)
            if str(name).casefold() == "applied_rows"
        ),
        None,
    )
    if applied_index is None:
        return False
    return all(
        isinstance(row, list)
        and applied_index < len(row)
        and _is_number(row[applied_index])
        and float(row[applied_index]) == 0.0
        for row in rows
    )


def _no_matching_rows_answer(
    question: str,
    schema: Mapping[str, Any],
) -> tuple[str, str]:
    atc_code = _atc4_code(question)
    atc_column = _find_column(_schema_columns(schema), r"atc\s*4")
    if atc_code and atc_column is not None:
        return (
            f"ATC4 열은 있으나 '{atc_code}' 조건에 맞는 행이 0건입니다.",
            f"ATC4={atc_code}",
        )
    return "요청한 조건에 맞는 행이 0건입니다.", "requested_filters"


def _source_column_labels(columns: Any, schema: Mapping[str, Any]) -> list[str]:
    if not isinstance(columns, list):
        return []
    raw_schema_columns = schema.get("columns")
    schema_columns = raw_schema_columns if isinstance(raw_schema_columns, list) else []
    names = {
        str(item.get("query_name") or ""): str(item.get("source_name") or "")
        for item in schema_columns
        if isinstance(item, dict)
        and str(item.get("query_name") or "")
        and str(item.get("source_name") or "")
    }
    labels: list[str] = []
    for value in columns:
        label = str(value)
        for query_name, source_name in names.items():
            label = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(query_name)}(?![A-Za-z0-9_])",
                source_name,
                label,
            )
        labels.append(label)
    return labels


def _session_payload(conversation_id: str, **values: Any) -> dict[str, Any]:
    return {
        "workflow_id": int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301")),
        "app_session_id": conversation_id,
        "chat_id": conversation_id,
        **values,
    }


def _message_content(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("planner response has no choices")
    message = choices[0].get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(choices[0].get("text"), str):
        return choices[0]["text"]
    raise ValueError("planner response has no content")


def _json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match is None:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("planner output must be a JSON object")
    return value


def _is_select_only_candidate(sql: str) -> bool:
    return bool(sql) and bool(re.match(r"^\s*(?:SELECT|WITH)\b", sql, re.IGNORECASE))


def _configured_terms(env_name: str, defaults: Sequence[str]) -> tuple[str, ...]:
    raw = os.getenv(env_name)
    if raw is None:
        return tuple(defaults)
    return tuple(term.strip() for term in raw.split(",") if term.strip())


def _contains_configured_term(text: str, env_name: str, defaults: Sequence[str]) -> bool:
    normalized = " ".join(text.casefold().split())
    for raw_term in _configured_terms(env_name, defaults):
        term = " ".join(raw_term.casefold().split())
        if term in {"합", "총"}:
            if re.search(
                rf"(?<![0-9a-z가-힣]){re.escape(term)}(?=$|[\s?.,!은는이가을를의])",
                normalized,
            ):
                return True
        elif term in normalized:
            return True
    return False


def _column_matches_question(
    column: Mapping[str, Any],
    question_tokens: Sequence[str],
    question_months: frozenset[str],
) -> bool:
    query_name = str(column.get("query_name") or "").casefold()
    source_name = str(column.get("source_name") or "").casefold()
    searchable = f"{query_name} {source_name}"
    question_text = " ".join(question_tokens)
    intent = _question_measure_intent(question_text)
    if intent == "amount" and (
        _is_average_column(source_name) or _is_quantity_column(source_name)
    ):
        return False
    if any(token in searchable for token in question_tokens):
        return True
    if question_months and question_months.intersection(month_keys(source_name)):
        return True
    if _is_amount_column(source_name) and not _is_average_column(source_name):
        if _contains_configured_term(
            question_text,
            "JW_CHAT_FILE_SQL_AMOUNT_QUESTION_TERMS",
            DEFAULT_AMOUNT_QUESTION_TERMS,
        ):
            return True
        if _requested_measure_label(question_text):
            return False
        return _is_aggregate_question(question_text)
    return False


def _measure_request(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
) -> MeasureRequest:
    label = _requested_measure_label(question)
    if label:
        normalized = re.sub(r"\s+", "", label).casefold()
        for schema in schemas:
            for column in _schema_columns(schema):
                source_name = re.sub(
                    r"\s+", "", str(column.get("source_name") or "")
                ).casefold()
                if normalized and normalized in source_name:
                    return MeasureRequest("recognized", label=label)
        return MeasureRequest("unsupported", label=label)
    intent = _question_measure_intent(question)
    if intent is not None:
        return MeasureRequest("recognized", intent=intent)
    return MeasureRequest("unspecified")


def _requested_measure_label(question: str) -> str:
    patterns = (
        r"([0-9A-Za-z가-힣_-]{2,}(?:율|률|금액|매출액|단가|수량|건수))\s*(?:합계|총계|합산|평균|총액|집계)",
        r"(?:합계|총계|합산|평균|총액|집계)(?:는|은|이|가)?\s*([0-9A-Za-z가-힣_-]{2,}(?:율|률|금액|매출액|단가|수량|건수))",
    )
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _missing_period(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
) -> str:
    requested = month_keys(question)
    if not requested:
        return ""
    available = frozenset(
        month
        for schema in schemas
        for column in _schema_columns(schema)
        for month in month_keys(str(column.get("source_name") or ""))
    )
    if not available:
        return ""
    missing = sorted(requested - available)
    if not missing:
        return ""
    year, month = missing[0].split("-", 1)
    return f"{year}년 {int(month)}월"


def _question_measure_intent(question: str) -> str | None:
    if _contains_configured_term(question, "JW_CHAT_FILE_SQL_AVERAGE_TERMS", DEFAULT_AVERAGE_TERMS):
        return "average"
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_COUNT_QUESTION_TERMS",
        DEFAULT_COUNT_QUESTION_TERMS,
    ):
        return "count"
    if _contains_configured_term(question, "JW_CHAT_FILE_SQL_QUANTITY_TERMS", DEFAULT_QUANTITY_TERMS):
        return "quantity"
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_AMOUNT_QUESTION_TERMS",
        DEFAULT_AMOUNT_QUESTION_TERMS,
    ):
        return "amount"
    return None


def _selected_columns_match_intent(
    intent: str,
    sql: str,
    schema: Mapping[str, Any],
) -> bool:
    raw_columns = schema.get("columns")
    columns = raw_columns if isinstance(raw_columns, list) else []
    source_names = {
        str(item.get("query_name") or "").casefold(): str(item.get("source_name") or "")
        for item in columns
        if isinstance(item, dict) and item.get("query_name")
    }
    targets = [
        (function.casefold(), source_names.get(query_name.casefold(), ""))
        for function, query_name in re.findall(
            r"\b(SUM|AVG)\s*\(\s*([A-Za-z][A-Za-z0-9_]*)\s*\)",
            sql,
            re.IGNORECASE,
        )
    ]
    targets = [(function, target) for function, target in targets if target]
    if intent == "count":
        return bool(
            re.search(
                r"\bCOUNT\s*\([^)]*\)\s+(?:AS\s+)?(?!applied_rows\b)[A-Za-z][A-Za-z0-9_]*",
                sql,
                re.IGNORECASE,
            )
        )
    if not targets:
        return False
    if intent == "amount":
        return all(
            function == "sum" and _is_amount_column(target)
            and not _is_average_column(target)
            and not _is_quantity_column(target)
            for function, target in targets
        )
    if intent == "average":
        return all(
            function == "avg" and _is_average_column(target)
            for function, target in targets
        )
    if intent == "quantity":
        return all(
            function == "sum" and _metric_family(target) == METRIC_QUANTITY
            for function, target in targets
        )
    return True


def _is_amount_column(name: str) -> bool:
    return _contains_configured_term(
        name,
        "JW_CHAT_FILE_SQL_AMOUNT_COLUMN_TERMS",
        DEFAULT_AMOUNT_COLUMN_TERMS,
    )


def _is_average_column(name: str) -> bool:
    return _contains_configured_term(name, "JW_CHAT_FILE_SQL_AVERAGE_TERMS", DEFAULT_AVERAGE_TERMS)


def _is_quantity_column(name: str) -> bool:
    return _contains_configured_term(name, "JW_CHAT_FILE_SQL_QUANTITY_TERMS", DEFAULT_QUANTITY_TERMS)


def _question_tokens(question: str) -> tuple[str, ...]:
    tokens = [
        token.casefold()
        for token in re.findall(r"[0-9A-Za-z가-힣_]{2,}", question)
    ]
    tokens.extend(month_keys(question))
    return tuple(dict.fromkeys(tokens))


def _markdown_cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _file_service_base_url() -> str:
    return os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")


def _file_service_timeout() -> float:
    return float(os.getenv("JW_CHAT_FILE_SQL_TIMEOUT_S", "5"))


def _planner_system_prompt() -> str:
    return os.getenv(
        "JW_CHAT_FILE_SQL_PLANNER_SYSTEM_PROMPT",
        (
            "You translate an uploaded-file question into one SQLite SELECT. "
            "Use exactly one supplied logical_name and query only its table alias data. "
            "Column query_name values (c1, c2, ...) are the only legal columns; "
            "source_name explains their meaning. Uploaded cell values are stored with TEXT "
            "affinity: compare categorical values with quoted string literals, and use "
            "SUM and AVG directly for numeric aggregates. Never use CAST because the scoped "
            "SQL policy rejects it. Never access system tables, attach databases, PRAGMA, "
            "operational marts, or other files. "
            "For every COUNT, SUM, AVG, grouped aggregate, or comparison query, also select "
            "COUNT(*) AS applied_rows so the result can be completeness-checked. "
            "Return JSON only as "
            '{"logical_name":"...","sql":"SELECT ... FROM data ..."}. '
            "If the question cannot be answered from these uploaded-file schemas, return "
            '{"logical_name":"","sql":""}.'
        ),
    )


def _planner_timeout() -> float:
    return float(os.getenv("JW_CHAT_FILE_SQL_PLANNER_TIMEOUT_S", "30"))


def _planner_max_tokens() -> int:
    return max(128, int(os.getenv("JW_CHAT_FILE_SQL_PLANNER_MAX_TOKENS", "2048")))


def _max_schema_tables() -> int:
    return max(1, int(os.getenv("JW_CHAT_FILE_SQL_MAX_TABLES", "4")))


def _max_schema_columns() -> int:
    return max(20, int(os.getenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "192")))


def _identity_column_count() -> int:
    return max(1, int(os.getenv("JW_CHAT_FILE_SQL_IDENTITY_COLUMNS", "24")))
