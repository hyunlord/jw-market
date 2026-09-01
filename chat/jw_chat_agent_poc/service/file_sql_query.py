from __future__ import annotations

import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import requests

from jw_chat_agent_poc.common.periods import (
    explicit_years,
    month_keys,
    months_back,
    quarter_keys,
    quarter_months,
    relative_span,
    year_months,
)
from jw_chat_agent_poc.genos_config import (
    resolve_planner_genos_base_url,
    resolve_planner_genos_token,
)
from jw_chat_agent_poc.orchestrator.unavailable_response import file_absence_answer
from jw_chat_agent_poc.service.actor_context import code_serving_actor_headers
from jw_chat_agent_poc.service.file_excel_analytics import query_file_analytics

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
_SOURCE_LOCATION_QUESTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:어느|어떤)\s*시트|(?:어느|어떤)\s*셀|시트.*셀)",
    re.IGNORECASE,
)


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
    detail: dict[str, Any] = field(default_factory=dict)


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


@dataclass(frozen=True, slots=True)
class GeneratedPlanResolution:
    plan: dict[str, str] | None
    attempts: int = 0
    failure_reason: str = ""
    schema_samples: tuple[dict[str, Any], ...] = ()


class GeneratedSqlValidationError(ValueError):
    """A stable, non-sensitive reason that generated SQL was not executable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class QueryIrResolution:
    ir: dict[str, Any] | None
    attempts: int = 0
    failure_reason: str = ""
    source: str = "deterministic"


_IR_CAPABILITIES_CACHE: OrderedDict[tuple[str, int | None, str, str], dict[str, Any]] = (
    OrderedDict()
)
_IR_CAPABILITIES_CACHE_MAX: Final = 128


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
    sql = ""
    current_stage = "capabilities"
    try:
        capabilities, cache_hit = _get_ir_capabilities(sources[0], conversation_id)
        if sources[0].document_id is not None:
            trace.append(
                {
                    "stage": "file_query_capabilities",
                    "status": "ok" if capabilities else "unavailable",
                    "cache_hit": str(cache_hit).lower(),
                    "dimension_count": str(
                        len(_ir_capability_values(capabilities, "dimensions"))
                    ),
                }
            )
        unsupported_axis = _unsupported_ir_axis(question, capabilities)
        if unsupported_axis:
            return _unsupported_ir_outcome(
                sources[0], capabilities, trace, unsupported_axis
            )
        if (
            capabilities
            and _should_route_query_ir(question)
            and not _prefer_existing_analytics_template(question)
        ):
            return _query_uploaded_ir(
                question,
                conversation_id,
                sources[0],
                capabilities,
                trace,
                route_reason=_query_ir_route_reason(question),
            )
        analytics = query_file_analytics(question, conversation_id, sources)
        if analytics is not None:
            return SqlQueryOutcome(
                file_context=analytics.file_context,
                file_source_items=_source_items(sources[:1]),
                errors=(),
                answer_md=analytics.answer_md,
                status=analytics.status,
                trace=(*trace, *analytics.trace, {
                    "stage": "file_query_route",
                    "status": "template",
                    "reason": "existing_template_matched",
                }),
                detail={
                    **analytics.detail,
                    "query_route": "template",
                    "query_route_reason": "existing_template_matched",
                },
            )
        current_stage = "schema"
        schemas = tuple(
            _fetch_schema(source, conversation_id)
            for source in sources[: _max_schema_tables()]
        )
        trace.append(
            {"stage": "schema", "status": "ok", "table_count": str(len(schemas))}
        )
        if _SOURCE_LOCATION_QUESTION_RE.search(question):
            scoped_sources = sources[: len(schemas)]
            answer = _render_source_location_answer(scoped_sources)
            trace.append(
                {
                    "stage": "source_location",
                    "status": "ok",
                    "sheet_count": str(len(scoped_sources)),
                }
            )
            return SqlQueryOutcome(
                file_context=answer,
                file_source_items=_source_items(scoped_sources),
                errors=(),
                answer_md=answer,
                trace=tuple(trace),
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
        plan_source = "deterministic"
        generation_attempts = 0
        generation_samples: tuple[dict[str, Any], ...] = ()
        if plan is None:
            untranslated = tuple(resolution.missing_slots) == ("요청한 조건",)
            if untranslated and capabilities:
                return _query_uploaded_ir(
                    question,
                    conversation_id,
                    sources[0],
                    capabilities,
                    trace,
                    route_reason="deterministic_template_miss",
                )
            generated = GeneratedPlanResolution(None)
            if plan is None:
                answer = (
                    "이 질문 형태는 아직 파일 집계로 변환하지 못했습니다."
                    if untranslated
                    else _missing_plan_answer(resolution.missing_slots, question=question)
                )
                trace.append(
                    {
                        "stage": "planner",
                        "status": "untranslated" if untranslated else "unsupported",
                        "resolved_slots": ",".join(resolution.resolved_slots),
                        "missing_slots": ",".join(resolution.missing_slots),
                        **(
                            {
                                "generation_attempts": str(generated.attempts),
                                "generation_failure": generated.failure_reason or "empty_plan",
                            }
                            if untranslated
                            else {}
                        ),
                        **_period_trace_fields(resolution.period, resolution.metric),
                    }
                )
                return SqlQueryOutcome(
                    file_context="## 업로드 파일 SQL 결과\n상태: 미지원\n" + answer,
                    file_source_items=_source_items(sources[: len(schemas)]),
                    errors=(
                        "file SQL plan unavailable"
                        if untranslated
                        else "file SQL deterministic plan unavailable",
                    ),
                    answer_md=answer,
                    status="untranslated" if untranslated else "unsupported_query",
                    trace=tuple(trace),
                    detail={
                        "generation_path": "query_ir_unavailable" if untranslated else "deterministic",
                        "generation_attempts": generated.attempts,
                        "generation_failure": generated.failure_reason,
                        "planner_schema_samples": list(generated.schema_samples),
                    },
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
                "plan_source": plan_source,
                "generation_attempts": str(generation_attempts),
                "schema_sample_query_count": str(len(generation_samples)),
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
        requested_families = _requested_measure_families(question)
        selected_columns_match = (
            _selected_columns_match_requested_families(
                requested_families,
                sql,
                schema,
            )
            if len(requested_families) > 1
            else not intent or _selected_columns_match_intent(intent, sql, schema)
        )
        if aggregate and not selected_columns_match:
            return _column_intent_failure(
                intent or "요청",
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
            plan_source,
            selected_columns,
        )
        current_stage = "execution"
        result = _run_query(conversation_id, logical_name, sql)
        trace.append({"stage": "execution", "status": "ok"})
        current_stage = "render"
        if _has_no_applied_rows(result):
            answer, filter_label = _no_matching_rows_answer(question, schema)
            detail = _sql_result_detail(source, sql, result)
            detail["generation_path"] = plan_source
            detail["generation_attempts"] = generation_attempts
            detail["planner_schema_samples"] = list(generation_samples)
            result_facts = dict(detail.get("result_facts") or {})
            result_facts["rows"] = ()
            detail["result_facts"] = result_facts
            trace.append(
                {
                    "stage": "render",
                    "status": "no_matching_rows",
                    "filter": filter_label,
                }
            )
            return SqlQueryOutcome(
                file_context="## 업로드 파일 SQL 결과\n상태: 조건 일치 0건\n" + answer,
                file_source_items=_source_items((source,)),
                errors=(),
                answer_md=answer,
                status="no_matching_rows",
                trace=tuple(trace),
                detail=detail,
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
            data_row_count = (
                _try_fetch_data_row_count(source, conversation_id)
                if _has_only_aggregate_row_exclusion(sql)
                else None
            )
            answer = _render_aggregate_answer(
                question,
                source,
                sql,
                result,
                schema,
                period=resolution.period,
                metric=resolution.metric,
                data_row_count=data_row_count,
            )
            if not answer:
                return _aggregate_contract_failure(
                    trace=(*trace, {"stage": "render", "status": "contract_failed"})
                )
        detail = _sql_result_detail(source, sql, result)
        detail["generation_path"] = plan_source
        detail["generation_attempts"] = generation_attempts
        detail["planner_schema_samples"] = list(generation_samples)
        if aggregate:
            auxiliary_aggregates = _run_auxiliary_aggregates(
                conversation_id=conversation_id,
                source=source,
                schema=schema,
                resolution=resolution,
                primary_sql=sql,
            )
            detail["auxiliary_aggregates"] = auxiliary_aggregates
            trace.append(
                {
                    "stage": "auxiliary_aggregates",
                    "status": "ok",
                    "query_count": str(len(auxiliary_aggregates)),
                    "success_count": str(
                        sum(item.get("status") == "ok" for item in auxiliary_aggregates)
                    ),
                }
            )
        return SqlQueryOutcome(
            file_context=context,
            file_source_items=_source_items((source,)),
            errors=(),
            answer_md=answer,
            trace=tuple(trace),
            detail=detail,
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
            detail=_sql_failure_detail(sources, sql, exc),
        )


def _get_ir_capabilities(
    source: SqlFileSource,
    conversation_id: str,
) -> tuple[dict[str, Any], bool]:
    if source.document_id is None:
        return {}, False
    key = (
        conversation_id,
        source.document_id,
        source.logical_name,
        source.sheet_name,
    )
    cached = _IR_CAPABILITIES_CACHE.get(key)
    if cached is not None:
        _IR_CAPABILITIES_CACHE.move_to_end(key)
        return dict(cached), True
    payload = _session_payload(
        conversation_id,
        document_id=source.document_id,
        logical_name=source.logical_name,
        sheet_name=source.sheet_name,
    )
    payload = {name: value for name, value in payload.items() if value not in (None, "")}
    try:
        response = requests.post(
            f"{_file_service_base_url()}/file-sql/capabilities",
            json=payload,
            headers=code_serving_actor_headers(),
            timeout=_ir_capabilities_timeout(),
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("capabilities"), dict):
            raise TypeError("file SQL capabilities response is malformed")
    except (requests.RequestException, ValueError, TypeError, KeyError):
        logger.warning(
            "file query capabilities unavailable logical_name=%s",
            source.logical_name,
            exc_info=True,
        )
        return {}, False
    _IR_CAPABILITIES_CACHE[key] = dict(body)
    _IR_CAPABILITIES_CACHE.move_to_end(key)
    while len(_IR_CAPABILITIES_CACHE) > _IR_CAPABILITIES_CACHE_MAX:
        _IR_CAPABILITIES_CACHE.popitem(last=False)
    return dict(body), False


def _ir_capability_values(capabilities: Mapping[str, Any], name: str) -> tuple[str, ...]:
    contract = capabilities.get("capabilities")
    if not isinstance(contract, Mapping):
        return ()
    values = contract.get(name)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(str(value) for value in values if str(value).strip())


_IR_DIMENSION_ALIASES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?:회사|판매사|제조사)별"), "MFR NAME KOR"),
    (re.compile(r"(?:브랜드|제품)별"), "PRODUCT NAME KOR"),
    (
        re.compile(r"(?:MOLECULE\s*DESC|성분)(?:\s*기준|별)", re.IGNORECASE),
        "MOLECULE DESC",
    ),
    (re.compile(r"ATC\s*1별", re.IGNORECASE), "ATC 1"),
    (re.compile(r"ATC\s*2별", re.IGNORECASE), "ATC 2"),
    (re.compile(r"ATC\s*3별", re.IGNORECASE), "ATC 3"),
    (re.compile(r"ATC\s*4별", re.IGNORECASE), "ATC 4"),
)
_IR_NON_DIMENSION_BY_TERMS: Final = {
    "기간",
    "날짜",
    "월",
    "분기",
    "연도",
    "년도",
    "시기",
}


def _requested_ir_dimensions(question: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            dimension
            for pattern, dimension in _IR_DIMENSION_ALIASES
            if pattern.search(question)
        )
    )


def _unsupported_ir_axis(
    question: str,
    capabilities: Mapping[str, Any],
) -> str:
    if not capabilities:
        return ""
    available = set(_ir_capability_values(capabilities, "dimensions"))
    for dimension in _requested_ir_dimensions(question):
        if dimension not in available:
            return dimension
    known_aliases = {match.group(1) for match in re.finditer(
        r"([0-9A-Za-z가-힣]+)별", question
    )}
    for token in sorted(known_aliases):
        if token in _IR_NON_DIMENSION_BY_TERMS:
            continue
        probe = f"{token}별"
        if any(pattern.search(probe) for pattern, _ in _IR_DIMENSION_ALIASES):
            continue
        return token
    return ""


def _should_route_query_ir(question: str) -> bool:
    dimensions = _requested_ir_dimensions(question)
    if dimensions:
        return True
    if re.search(r"sell\s*(?:in|out)\s*price|(?:판매|매입)\s*단가", question, re.IGNORECASE):
        return True
    years = tuple(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", question)))
    return len(years) >= 2 and bool(re.search(r"대비|비교|증감|성장", question))


def _prefer_existing_analytics_template(question: str) -> bool:
    """Keep the established ATC share-and-growth contract on its exact template."""

    return bool(
        _question_ir_atc_code(question)
        and re.search(r"성장|CAGR|추이", question, re.IGNORECASE)
    )


def _query_ir_route_reason(question: str) -> str:
    dimensions = _requested_ir_dimensions(question)
    if len(dimensions) > 1:
        return "multi_dimension_grouping"
    if dimensions and _question_ir_atc_code(question):
        return "dimension_with_filter"
    if dimensions:
        return "dimension_grouping"
    return "period_comparison"


def _unsupported_ir_outcome(
    source: SqlFileSource,
    capabilities: Mapping[str, Any],
    trace: Sequence[Mapping[str, str]],
    unsupported_axis: str,
) -> SqlQueryOutcome:
    dimensions = _ir_capability_values(capabilities, "dimensions")
    available = ", ".join(dimensions) if dimensions else "확인 가능한 축 없음"
    answer = (
        f"이 파일에는 {unsupported_axis} 차원이 없습니다. "
        f"가능한 축은 {available}입니다."
    )
    route_trace = {
        "stage": "file_query_route",
        "status": "unsupported_dimension",
        "reason": "capability_dimension_missing",
        "requested_axis": unsupported_axis,
    }
    return SqlQueryOutcome(
        file_context="## 업로드 파일 질의 결과\n상태: 미지원\n" + answer,
        file_source_items=_source_items((source,)),
        errors=("file query IR unsupported dimension",),
        answer_md=answer,
        status="unsupported_dimension",
        trace=(*trace, route_trace),
        detail={
            "generation_path": "query_ir",
            "query_route": "unsupported",
            "query_route_reason": "capability_dimension_missing",
            "requested_axis": unsupported_axis,
            "capabilities": dict(capabilities),
        },
    )


def _deterministic_query_ir(
    question: str,
    source: SqlFileSource,
    capabilities: Mapping[str, Any],
) -> dict[str, Any] | None:
    dimensions = _requested_ir_dimensions(question)
    available_dimensions = set(_ir_capability_values(capabilities, "dimensions"))
    available_measures = set(_ir_capability_values(capabilities, "measures"))
    price_question = bool(
        re.search(
            r"sell\s*(?:in|out)\s*price|(?:판매|매입)\s*단가",
            question,
            re.IGNORECASE,
        )
    )
    if price_question and not dimensions and "PRODUCT NAME KOR" in available_dimensions:
        dimensions = ("PRODUCT NAME KOR",)
    if not dimensions or not set(dimensions) <= available_dimensions:
        return None
    measures: list[dict[str, str]] = []
    if price_question:
        requested_prices = (
            ("SELL IN PRICE", "Sellin price", r"sell\s*in\s*price|매입\s*단가"),
            (
                "SELL OUT PRICE AVERAGE",
                "Sellout price",
                r"sell\s*out\s*price|판매\s*단가",
            ),
        )
        measures.extend(
            {"source": source_name, "aggregation": "avg", "alias": alias}
            for source_name, alias, pattern in requested_prices
            if source_name in available_measures
            and re.search(pattern, question, re.IGNORECASE)
        )
    else:
        asks_sales = bool(re.search(r"매출|판매액|금액|sales?", question, re.IGNORECASE))
        asks_units = bool(re.search(r"수량|units?|volume", question, re.IGNORECASE))
        if (asks_sales or not asks_units) and "VALUES LC SI PRICE" in available_measures:
            measures.append(
                {"source": "VALUES LC SI PRICE", "aggregation": "sum", "alias": "매출"}
            )
        if asks_units and "UNITS" in available_measures:
            measures.append({"source": "UNITS", "aggregation": "sum", "alias": "수량"})
        needs_share = bool(
            re.search(r"점유율|M\s*/\s*S|market\s*share", question, re.IGNORECASE)
        ) or bool(_question_ir_atc_code(question) and "PRODUCT NAME KOR" in dimensions)
        if needs_share and "VALUES LC SI PRICE" in available_measures:
            measures.append(
                {"source": "VALUES LC SI PRICE", "aggregation": "share", "alias": "M/S"}
            )
    if not measures:
        return None
    years = tuple(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", question)))
    filters: list[dict[str, Any]] = []
    atc_code = _question_ir_atc_code(question)
    if atc_code:
        code = atc_code.upper()
        level = min(max(len(code) - 1, 1), 4)
        column = f"ATC {level}"
        if column in available_dimensions:
            filters.append({"col": column, "op": "like", "val": f"{code}%"})
    if price_question:
        brand = _price_brand_candidate(question)
        if brand:
            filters.append(
                {"col": "PRODUCT NAME KOR", "op": "eq", "val": brand}
            )
    alias = measures[0]["alias"]
    compare: dict[str, Any] | None = None
    order_by = alias
    if len(years) >= 2 and re.search(r"대비|비교|성장|증감", question):
        compare = {
            "mode": "period",
            "base": years[0],
            "target": years[-1],
            "dimension": None,
        }
        order_by = f"{alias}_growth_pct"
    period = {
        "grain": "year",
        "range": [],
        "list": list(years),
    } if years else None
    return {
        **_session_payload(
            "",
            document_id=source.document_id,
            logical_name=source.logical_name,
            sheet_name=source.sheet_name,
        ),
        "dimensions": list(dimensions),
        "measures": measures,
        "filters": filters,
        "period": period,
        "compare": compare,
        "order": [{"by": order_by, "dir": "desc"}],
        "limit": (
            int((capabilities.get("capabilities") or {}).get("max_rows") or 1000)
            if atc_code and "PRODUCT NAME KOR" in dimensions and not compare
            else 100 if compare else 50
        ),
        "limit_scope": "rows",
    }


def _price_brand_candidate(question: str) -> str:
    prefix = re.split(
        r"sell\s*(?:in|out)\s*price|(?:판매|매입)\s*단가",
        question,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    prefix = re.sub(r"^.*?이\s*파일에서\s*", "", prefix).strip()
    token = prefix.split()[-1] if prefix else ""
    return re.sub(r"(?:의|은|는)$", "", token)


def _question_ir_atc_code(question: str) -> str:
    explicit = re.search(
        r"(?:ATC\s*[1-4]?\s*(?:기준\s*)?)?\b([A-Z]\d{2}[A-Z0-9]?)(?=$|[^A-Z0-9])",
        question,
        re.IGNORECASE,
    )
    return explicit.group(1).upper() if explicit else ""


def _resolve_query_ir(
    question: str,
    conversation_id: str,
    source: SqlFileSource,
    capabilities: Mapping[str, Any],
) -> QueryIrResolution:
    deterministic = _deterministic_query_ir(question, source, capabilities)
    if deterministic is not None:
        deterministic.update(_session_payload(conversation_id))
        _validate_query_ir(deterministic, capabilities)
        return QueryIrResolution(deterministic, source="deterministic")
    feedback = ""
    for attempt in range(1, 3):
        try:
            candidate = _generate_query_ir(question, source, capabilities, feedback)
            if candidate is None:
                return QueryIrResolution(None, attempt, "empty_ir", "planner")
            candidate.update(_session_payload(conversation_id))
            _validate_query_ir(candidate, capabilities)
            return QueryIrResolution(candidate, attempt, source="planner")
        except GeneratedSqlValidationError as exc:
            feedback = exc.reason
        except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
            return QueryIrResolution(
                None, attempt, _failure_reason(exc), "planner"
            )
    return QueryIrResolution(None, 2, feedback or "invalid_ir", "planner")


def _generate_query_ir(
    question: str,
    source: SqlFileSource,
    capabilities: Mapping[str, Any],
    validation_feedback: str = "",
) -> dict[str, Any] | None:
    token = resolve_planner_genos_token()
    if not token:
        raise RuntimeError("planner token is unavailable")
    request: dict[str, Any] = {
        "question": question,
        "file": {
            "logical_name": source.logical_name,
            "document_id": source.document_id,
            "sheet_name": source.sheet_name,
        },
        "capabilities": capabilities.get("capabilities") or {},
        "contract": {
            "dimensions": "0..max_dimensions detected names",
            "measures": "1..max_measures {source,aggregation,alias}",
            "filters": "{col,op,val}",
            "period": "{grain,range,list} or null",
            "compare": "{mode,base,target,dimension} or null",
            "order": "[{by,dir}]",
            "limit_scope": "rows or entities",
        },
    }
    if validation_feedback:
        request["validation_feedback"] = validation_feedback
    response = requests.post(
        f"{resolve_planner_genos_base_url().rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return one JSON query IR only. Never write SQL or include a sql field. "
                        "Use only dimensions, measures, grains, operators, aggregations, and limits "
                        "listed in capabilities. Preserve file identifiers from the request."
                    ),
                },
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": _planner_max_tokens(),
        },
        timeout=_planner_timeout(),
    )
    response.raise_for_status()
    parsed = _json_object(_message_content(response.json()))
    return parsed or None


def _validate_query_ir(
    ir: Mapping[str, Any],
    capabilities: Mapping[str, Any],
) -> None:
    if "sql" in ir:
        raise GeneratedSqlValidationError("sql_field_forbidden")
    contract = capabilities.get("capabilities")
    if not isinstance(contract, Mapping):
        raise GeneratedSqlValidationError("capabilities_missing")
    dimensions = ir.get("dimensions") or []
    measures = ir.get("measures") or []
    filters = ir.get("filters") or []
    if not isinstance(dimensions, list) or not isinstance(measures, list):
        raise GeneratedSqlValidationError("invalid_ir_shape")
    if not set(map(str, dimensions)) <= set(_ir_capability_values(capabilities, "dimensions")):
        raise GeneratedSqlValidationError("unknown_dimension")
    if not measures:
        raise GeneratedSqlValidationError("measure_required")
    allowed_measures = set(_ir_capability_values(capabilities, "measures"))
    allowed_aggregations = set(_ir_capability_values(capabilities, "supported_aggregations"))
    for measure in measures:
        if not isinstance(measure, Mapping) or str(measure.get("source")) not in allowed_measures:
            raise GeneratedSqlValidationError("unknown_measure")
        if str(measure.get("aggregation")) not in allowed_aggregations:
            raise GeneratedSqlValidationError("unsupported_aggregation")
    allowed_operators = set(_ir_capability_values(capabilities, "supported_operators"))
    for item in filters:
        if not isinstance(item, Mapping) or str(item.get("col")) not in set(
            _ir_capability_values(capabilities, "dimensions")
        ):
            raise GeneratedSqlValidationError("unknown_filter_column")
        if str(item.get("op")) not in allowed_operators:
            raise GeneratedSqlValidationError("unsupported_operator")
    limit = ir.get("limit", 50)
    max_rows = int(contract.get("max_rows") or 1000)
    if not isinstance(limit, int) or limit < 1 or limit > max_rows:
        raise GeneratedSqlValidationError("row_limit_exceeded")


def _run_query_ir(ir: Mapping[str, Any]) -> dict[str, Any]:
    response = requests.post(
        f"{_file_service_base_url()}/file-sql/query-ir",
        json=dict(ir),
        headers=code_serving_actor_headers(),
        timeout=max(_file_service_timeout(), 75.0),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError("file query IR response must be an object")
    return body


def _query_uploaded_ir(
    question: str,
    conversation_id: str,
    source: SqlFileSource,
    capabilities: Mapping[str, Any],
    trace: Sequence[Mapping[str, str]],
    *,
    route_reason: str,
) -> SqlQueryOutcome:
    resolution = _resolve_query_ir(question, conversation_id, source, capabilities)
    route_trace = {
        "stage": "file_query_route",
        "status": "query_ir" if resolution.ir else "ir_generation_failed",
        "reason": route_reason,
        "ir_source": resolution.source,
        "ir_attempts": str(resolution.attempts),
    }
    if resolution.ir is None:
        answer = (
            "이 복합 질의를 안전한 파일 조회로 변환하지 못했습니다. "
            "회사, 브랜드, ATC 분류, 기간과 측정치를 명시해 다시 질문해 주세요."
        )
        return SqlQueryOutcome(
            file_context="## 업로드 파일 질의 결과\n상태: 미지원\n" + answer,
            file_source_items=_source_items((source,)),
            errors=("file query IR generation failed",),
            answer_md=answer,
            status="untranslated",
            trace=(*trace, route_trace),
            detail={
                "generation_path": "query_ir",
                "query_route": "query_ir",
                "query_route_reason": route_reason,
                "generation_failure": resolution.failure_reason,
                "capabilities": dict(capabilities),
            },
        )
    result = _run_query_ir(resolution.ir)
    status = str(result.get("status") or "")
    if status != "ok":
        message = str(result.get("message") or "질의를 실행할 수 없습니다.")
        candidates = result.get("candidates")
        candidate_text = ", ".join(map(str, candidates)) if isinstance(candidates, list) else ""
        answer = message + (f" 가능한 값은 {candidate_text}입니다." if candidate_text else "")
        return SqlQueryOutcome(
            file_context="## 업로드 파일 질의 결과\n상태: 미지원\n" + answer,
            file_source_items=_source_items((source,)),
            errors=(f"file query IR rejected: {result.get('error_code') or 'unknown'}",),
            answer_md=answer,
            status="unsupported_query",
            trace=(*trace, route_trace, {
                "stage": "query_ir_execution",
                "status": "rejected",
                "reason": str(result.get("error_code") or "unknown"),
            }),
            detail={
                "generation_path": "query_ir",
                "query_route": "query_ir",
                "query_route_reason": route_reason,
                "query_ir": resolution.ir,
                "capabilities": dict(capabilities),
                "ir_response": result,
            },
        )
    columns = [str(value) for value in result.get("columns") or ()]
    rows = [row for row in result.get("rows") or () if isinstance(row, list)]
    display_limit = 40
    table = _render_query_ir_table(columns, rows[:display_limit])
    if len(rows) > display_limit:
        table += (
            f"\n전체 {len(rows)}건 중 {display_limit}건 표시 · "
            "나머지는 조회 상세에서 확인"
        )
    caption = str(result.get("sheet_selection_caption") or "").strip()
    answer = _render_query_ir_answer(columns, rows, caption)
    analytics_response = dict(result)
    analytics_response["operation"] = "query_ir"
    detail = {
        "generation_path": "query_ir",
        "query_route": "query_ir",
        "query_route_reason": route_reason,
        "query_ir": resolution.ir,
        "executed_sql": str(result.get("executed_sql") or ""),
        "executed_sql_statements": result.get("executed_sql_statements") or [],
        "display_sql": str(result.get("executed_sql") or ""),
        "capabilities": dict(capabilities),
        "analytics_response": analytics_response,
        "analytics_table_markdown": table,
        "result_facts": {"columns": columns, "rows": rows, "applied_rows": len(rows)},
        "table_mapping": result.get("table_mapping") or [],
        "sheet_table_map": result.get("sheet_table_map") or [],
        "aggregation_summary": result.get("aggregation_summary"),
        "by_query": [
            {
                "query": str(measure.get("alias") or measure.get("source") or ""),
                "source": str(measure.get("source") or ""),
                "aggregation": str(measure.get("aggregation") or ""),
                "result_rows": len(rows),
            }
            for measure in resolution.ir.get("measures") or ()
            if isinstance(measure, Mapping)
        ],
    }
    return SqlQueryOutcome(
        file_context=(
            "## 업로드 파일 복합 질의 결과\n"
            + (caption + "\n" if caption else "")
            + answer
            + "\n\n"
            + table
        ),
        file_source_items=_source_items((source,)),
        errors=(),
        answer_md=answer,
        status="ok",
        trace=(*trace, route_trace, {
            "stage": "query_ir_execution",
            "status": "ok",
            "result_rows": str(len(rows)),
        }),
        detail=detail,
    )


def _render_query_ir_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not columns:
        return ""
    header = "| " + " | ".join(_markdown_cell(value) for value in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(_markdown_cell(value) for value in row[: len(columns)]) + " |"
        for row in rows
    ]
    return "\n".join((header, divider, *body))


def _render_query_ir_answer(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    caption: str,
) -> str:
    if not rows:
        return "요청한 조건과 일치하는 파일 집계 결과가 0건입니다."
    first = rows[0]
    pairs = ", ".join(
        f"{column} {_markdown_cell(first[index])}"
        for index, column in enumerate(columns[:4])
        if index < len(first)
    )
    prefix = f"{caption} " if caption else ""
    totals: list[str] = []
    for measure_name in ("매출", "수량"):
        if measure_name not in columns:
            continue
        measure_index = columns.index(measure_name)
        values = [
            row[measure_index]
            for row in rows
            if measure_index < len(row) and _is_number(row[measure_index])
        ]
        if values:
            totals.append(f"{measure_name} {_format_number(sum(values))}")
    total_text = f" 카테고리 합계는 {', '.join(totals)}입니다." if totals else ""
    return (
        f"{prefix}복합 질의 결과는 {len(rows):,}행이며, 첫 행은 {pairs}입니다."
        f"{total_text}"
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
        if source.sheet_name:
            item["sheet_name"] = source.sheet_name
        items.append(item)
    return tuple(items)


def _sql_result_detail(
    source: SqlFileSource,
    sql: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    columns = tuple(str(column) for column in result.get("columns", ()) if str(column))
    raw_rows = result.get("rows")
    rows = tuple(
        tuple(row) for row in raw_rows if isinstance(row, (list, tuple))
    ) if isinstance(raw_rows, list) else ()
    row_count = result.get("row_count")
    total_row_count = row_count if isinstance(row_count, int) and row_count >= 0 else len(rows)
    sampled_rows = rows[: _file_detail_row_limit()]
    result_facts = _sql_result_facts(sql, columns, rows, sampled_rows)
    aggregates: dict[str, Any] = {}
    if rows:
        for index, column in enumerate(columns):
            if (
                re.search(
                    r"(?:total|sum|count|avg|average|applied_rows|previous|current|change|maximum|period_)",
                    column,
                    re.IGNORECASE,
                )
                and index < len(rows[0])
            ):
                aggregates[column] = rows[0][index]
    return {
        "executed_sql": sql,
        "table_mapping": _table_mapping((source,)),
        "columns": columns,
        "rows": sampled_rows,
        "total_row_count": total_row_count,
        "aggregate_values": aggregates,
        "result_facts": result_facts,
    }


def _sql_result_facts(
    sql: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    sampled_rows: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    if not columns or not rows:
        return {"kind": "single_value", "columns": tuple(columns), "rows": ()}

    lowered = tuple(column.casefold() for column in columns)
    applied_index = next(
        (index for index, column in enumerate(lowered) if column == "applied_rows"),
        None,
    )
    period_columns = tuple(
        (f"{match.group(1)}-{match.group(2)}", index)
        for index, column in enumerate(lowered)
        if (
            match := re.fullmatch(
                r"period_(20\d{2})_(0[1-9]|1[0-2])",
                column,
            )
        )
        is not None
    )
    if len(period_columns) >= 2:
        first_row = rows[0]
        period_indices = {index for _period, index in period_columns}
        dimension_index = next(
            (
                index
                for index in range(len(columns))
                if index != applied_index and index not in period_indices
            ),
            None,
        )
        periods = tuple(
            {"period": period, "value": first_row[index]}
            for period, index in period_columns
            if index < len(first_row) and _is_number(first_row[index])
        )
        facts: dict[str, Any] = {
            "kind": "period_comparison" if len(periods) == 2 else "time_series",
            "columns": tuple(columns),
            "periods": periods,
            "rows": tuple(tuple(row) for row in sampled_rows),
        }
        if dimension_index is not None and dimension_index < len(first_row):
            facts["label"] = str(first_row[dimension_index])
        if (
            applied_index is not None
            and applied_index < len(first_row)
            and _is_number(first_row[applied_index])
        ):
            facts["applied_rows"] = first_row[applied_index]
        if len(periods) >= 2:
            first_value = periods[0]["value"]
            last_value = periods[-1]["value"]
            change = last_value - first_value
            facts["change_value"] = change
            if first_value != 0:
                facts["change_pct"] = round(change * 100 / first_value, 2)
        return facts

    value_indices = tuple(
        index
        for index, column in enumerate(columns)
        if index != applied_index
        and column.casefold() != "scope_total_value"
        and re.search(
            r"(?:total|sum|value|amount|quantity|sales|revenue|count|avg|average|maximum)",
            column,
            re.IGNORECASE,
        )
    )
    value_index = value_indices[0] if value_indices else None
    scope_total_index = next(
        (
            index
            for index, column in enumerate(columns)
            if column.casefold() == "scope_total_value"
        ),
        None,
    )
    dimension_index = next(
        (
            index
            for index in range(len(columns))
            if index != applied_index and index not in value_indices
        ),
        None,
    )
    if value_index is not None and dimension_index is not None:
        numeric_values = tuple(
            row[value_index]
            for row in rows
            if value_index < len(row) and _is_number(row[value_index])
        )
        scope_totals = tuple(
            row[scope_total_index]
            for row in rows
            if scope_total_index is not None
            and scope_total_index < len(row)
            and _is_number(row[scope_total_index])
        )
        total = scope_totals[0] if scope_totals else sum(numeric_values)
        output_rows: list[dict[str, Any]] = []
        for rank, row in enumerate(sampled_rows, start=1):
            if max(dimension_index, value_index) >= len(row):
                continue
            value = row[value_index]
            if not _is_number(value):
                continue
            item: dict[str, Any] = {
                "rank": rank,
                "label": str(row[dimension_index]),
                "value": value,
            }
            if (
                applied_index is not None
                and applied_index < len(row)
                and _is_number(row[applied_index])
            ):
                item["applied_rows"] = row[applied_index]
            if total:
                item["composition_pct"] = round(value * 100 / total, 2)
            output_rows.append(item)
        applied_values = tuple(
            row[applied_index]
            for row in rows
            if applied_index is not None
            and applied_index < len(row)
            and _is_number(row[applied_index])
        )
        return {
            "kind": (
                "top_n"
                if re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE)
                else "group_by"
            ),
            "columns": tuple(columns),
            "dimension": columns[dimension_index],
            "value_column": columns[value_index],
            "rows": tuple(output_rows),
            "result_total_value": total,
            "applied_rows": sum(applied_values),
        }

    first_row = rows[0]
    value = (
        first_row[value_index]
        if value_index is not None
        and value_index < len(first_row)
        and _is_number(first_row[value_index])
        else None
    )
    facts = {
        "kind": "single_value",
        "columns": tuple(columns),
        "rows": tuple(tuple(row) for row in sampled_rows),
        "value": value,
        "values": {
            columns[index]: first_row[index]
            for index in value_indices
            if index < len(first_row) and _is_number(first_row[index])
        },
    }
    if (
        applied_index is not None
        and applied_index < len(first_row)
        and _is_number(first_row[applied_index])
    ):
        facts["applied_rows"] = first_row[applied_index]
    return facts


def _run_auxiliary_aggregates(
    *,
    conversation_id: str,
    source: SqlFileSource,
    schema: Mapping[str, Any],
    resolution: DeterministicPlanResolution,
    primary_sql: str,
) -> tuple[dict[str, Any], ...]:
    specs = _auxiliary_aggregate_specs(schema, resolution, primary_sql)
    output: list[dict[str, Any]] = []
    for kind, sql in specs:
        try:
            result = _run_query(conversation_id, source.logical_name, sql)
        except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
            logger.warning(
                "file SQL auxiliary aggregate unavailable logical_name=%s kind=%s reason=%s",
                source.logical_name,
                kind,
                _failure_reason(exc),
            )
            output.append(
                {
                    "kind": kind,
                    "status": "error",
                    **_sql_failure_detail((source,), sql, exc),
                }
            )
            continue
        output.append(
            {
                "kind": kind,
                "status": "ok",
                **_sql_result_detail(source, sql, result),
            }
        )
    return tuple(output)


def _auxiliary_aggregate_specs(
    schema: Mapping[str, Any],
    resolution: DeterministicPlanResolution,
    primary_sql: str,
) -> tuple[tuple[str, str], ...]:
    metric = resolution.metric
    period = resolution.period
    if (
        metric is None
        or period is None
        or not period.months
        or not _has_only_aggregate_row_exclusion(primary_sql)
    ):
        return ()

    columns = _schema_columns(schema)
    row_exclusion = _aggregate_row_exclusion(columns)
    if not row_exclusion:
        return ()
    total_expression = _period_sum_expression(metric, period.months)
    if not total_expression:
        return ()

    specs: list[tuple[str, str]] = []
    manufacturer = _find_column(
        columns,
        r"(?:^|\b)(?:mfr|manufacturer|company)(?:\b|$)|제조사|업체",
    )
    if manufacturer is not None:
        query_name = str(manufacturer.get("query_name") or "")
        if query_name:
            specs.append(
                (
                    "manufacturer_top3",
                    (
                        f"SELECT {query_name} AS manufacturer, {total_expression}, "
                        f"COUNT(*) AS applied_rows FROM data WHERE {row_exclusion} "
                        f"AND {query_name} IS NOT NULL AND TRIM({query_name}) <> '' "
                        f"GROUP BY {query_name} ORDER BY total_value DESC LIMIT 3"
                    ),
                )
            )

    if len(period.months) == 1:
        current_period = period.months[0]
        available = tuple(metric.columns)
        current_index = next(
            (index for index, item in enumerate(available) if item[0] == current_period),
            -1,
        )
        if current_index > 0:
            _previous_period, previous_query = available[current_index - 1]
            _current_period, current_query = available[current_index]
            specs.append(
                (
                    "previous_period_change",
                    (
                        f"SELECT SUM({previous_query}) AS previous_value, "
                        f"SUM({current_query}) AS current_value, "
                        f"(SUM({current_query}) - SUM({previous_query})) AS change_value, "
                        f"COUNT(*) AS applied_rows FROM data WHERE {row_exclusion}"
                    ),
                )
            )

    product = _find_column(
        columns,
        r"(?:^|\b)product(?:\s+name)?(?:\b|$)|제품(?:명)?",
    )
    if product is not None:
        query_name = str(product.get("query_name") or "")
        if query_name:
            specs.append(
                (
                    "maximum_product",
                    (
                        f"SELECT {query_name} AS product, {total_expression}, "
                        f"COUNT(*) AS applied_rows FROM data WHERE {row_exclusion} "
                        f"AND {query_name} IS NOT NULL AND TRIM({query_name}) <> '' "
                        f"GROUP BY {query_name} ORDER BY total_value DESC LIMIT 1"
                    ),
                )
            )
    return tuple(specs[:3])


def _sql_failure_detail(
    sources: Sequence[SqlFileSource],
    sql: str,
    exc: Exception,
) -> dict[str, Any]:
    message = f"{type(exc).__name__}: {exc}"
    masked = re.sub(
        r"(?i)(authorization|token|api[_-]?key|password|secret)\s*[:=]\s*[^\s,&]+",
        r"\1=[MASKED]",
        message,
    )
    masked = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[MASKED]@", masked)
    return {
        "executed_sql": sql,
        "table_mapping": _table_mapping(sources),
        "error": masked,
    }


def _table_mapping(sources: Sequence[SqlFileSource]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "file_name": source.file_name,
            "sheet_name": source.sheet_name,
            "logical_name": source.logical_name,
        }
        for source in sources
    )


def _file_detail_row_limit() -> int:
    try:
        return max(1, int(os.getenv("FILE_DETAIL_ROW_LIMIT", "10")))
    except ValueError:
        return 10


def _render_source_location_answer(sources: Sequence[SqlFileSource]) -> str:
    lines = [
        "## 업로드 파일 위치",
        "| 파일 | 시트 | 셀 위치 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {source.file_name} | {source.sheet_name} | "
        "파일 SQL 레코드에는 원본 셀 주소가 기록되지 않음 |"
        for source in sources
    )
    return "\n".join(lines)


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
    excluded_total_row_count: int | None = 0,
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
    if excluded_total_row_count is None:
        lines.append(
            "집계 제외: 식별 차원이 모두 비어 있는 합계 성격 행 필터 적용 "
            "(제외 건수 미상 · 파일에는 그대로 남아 있으며 조회 상세에서 확인할 수 있습니다)"
        )
    elif excluded_total_row_count > 0:
        lines.append(
            f"집계 제외: 식별 차원이 모두 비어 있는 합계 성격 행 {excluded_total_row_count:,}건 "
            "(파일에는 그대로 남아 있으며 조회 상세에서 확인할 수 있습니다)"
        )
    return lines


def _public_metric_label(metric: "MetricScope", schema: Mapping[str, Any]) -> str:
    source_columns = {
        str(item.get("query_name") or ""): item
        for item in _schema_columns(schema)
    }
    bases = {
        _measure_basis_for_column(source_columns.get(query_name, {}), schema)
        for _, query_name in metric.columns
    }
    if "sell_out" in bases:
        return "sell-out 기준 금액"
    if "sell_in" in bases:
        return "sell-in 기준 금액"
    names = " ".join(
        str(source_columns.get(query_name, {}).get("source_name") or "")
        for _, query_name in metric.columns
    ).casefold()
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


def _format_aggregate_value(
    value: Any,
    *,
    amount: bool,
    exact_won: bool = False,
) -> str:
    if not _is_number(value):
        return _markdown_cell(value)
    if amount:
        eok = float(value) / 100_000_000
        eok_text = f"{eok:,.2f}"
        if eok_text.endswith("0"):
            eok_text = eok_text[:-1]
        display = f"{eok_text}억원"
        if exact_won:
            return f"{display} ({_format_number(value)}원)"
        return display
    return _format_number(value)


def _format_single_amount_total(
    value: Any,
    *,
    amount: bool,
    disclose_exact_won: bool,
) -> str:
    rendered = _format_aggregate_value(value, amount=amount)
    if not disclose_exact_won or not amount or not _is_number(value):
        return rendered
    return f"{rendered} ({_format_number(value)}원)"


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
    data_row_count: int | None = None,
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
            f"시트·테이블명: `{source.sheet_name}` / data",
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
            f"시트·테이블명: `{source.sheet_name}` / data",
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
        f"시트·테이블명: `{source.sheet_name}` / data",
        f"필터 조건: {filter_text}",
        *_scope_disclosure_lines(
            period,
            metric,
            schema,
            excluded_total_row_count=(
                _excluded_aggregate_row_count(data_row_count, total_applied, sql)
                if _TOTAL_ROW_EXCLUSION_RE.search(sql) is not None
                else 0
            ),
        ),
        "사용 열: " + (_column_list_text(used_columns) if used_columns else "집계 결과 열"),
        "집계 함수: " + ", ".join(aggregate_functions),
        f"적용 행 수: {_format_number(total_applied)}",
        "| " + " | ".join(labels) + " |",
        "| " + " | ".join("---" for _ in labels) + " |",
    ]
    normalized_sheet_name = re.sub(r"[-_\s]+", " ", source.sheet_name.casefold()).strip()
    disclose_exact_won = "sell out" in normalized_sheet_name
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    _format_single_amount_total(
                        value,
                        amount=index in amount_indices,
                        disclose_exact_won=disclose_exact_won,
                    )
                    if len(rows) == 1
                    else _format_aggregate_value(value, amount=index in amount_indices)
                )
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
            channel = _channel_dimension_column(columns)
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
        requested_families = _requested_measure_families(question)
        amount_and_quantity = {
            METRIC_AMOUNT,
            METRIC_QUANTITY,
        }.issubset(requested_families)
        intent = (
            "amount"
            if amount_and_quantity
            else _question_measure_intent(question) or "amount"
        )
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
            else (
                _metric_scope_for_family(columns, METRIC_AMOUNT)
                if amount_and_quantity
                else _resolve_metric_scope(question, columns)
            )
        )
        period_scope = (
            _resolve_period_scope(question, metric_scope.available_months)
            if metric_scope is not None
            else None
        )
        quantity_metric_scope = (
            _metric_scope_for_family(columns, METRIC_QUANTITY)
            if amount_and_quantity
            else None
        )
        quantity_period_scope = (
            _resolve_period_scope(
                question,
                quantity_metric_scope.available_months,
            )
            if quantity_metric_scope is not None
            else None
        )
        explicit_period_comparison = _is_explicit_period_comparison(question)
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
        elif amount_and_quantity and (
            metric_scope is None or quantity_metric_scope is None
        ):
            if metric_scope is None:
                missing.append("금액")
            if quantity_metric_scope is None:
                missing.append("수량")
            aggregate_expression = ""
            aggregate_alias = "total_value"
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
        elif (
            explicit_period_comparison
            and period_scope is not None
            and len(period_scope.months) < 2
        ):
            missing.append(
                f"비교 요청 기간({period_scope.request_label})"
                if period_scope.request_label
                else "비교 요청 기간"
            )
            aggregate_expression = ""
            aggregate_alias = "total_value"
        elif (
            amount_and_quantity
            and quantity_period_scope is not None
            and quantity_period_scope.status == "unresolved"
        ):
            missing.append(
                f"수량 요청 기간({quantity_period_scope.request_label})"
                if quantity_period_scope.request_label
                else "수량 요청 기간"
            )
            aggregate_expression = ""
            aggregate_alias = "total_value"
        elif (
            amount_and_quantity
            and metric_scope is not None
            and period_scope is not None
            and quantity_metric_scope is not None
            and quantity_period_scope is not None
        ):
            amount_expression = _period_sum_expression(
                metric_scope,
                period_scope.months,
                alias="total_value",
            )
            quantity_expression = _period_sum_expression(
                quantity_metric_scope,
                quantity_period_scope.months,
                alias="total_quantity",
            )
            aggregate_expression = ", ".join(
                expression
                for expression in (amount_expression, quantity_expression)
                if expression
            )
            aggregate_alias = "total_value"
            if amount_expression and quantity_expression:
                resolved.extend(("amount_measure", "quantity_measure", "period"))
            else:
                missing.append("금액·수량")
        elif (
            metric_scope is not None
            and period_scope is not None
            and explicit_period_comparison
            and len(period_scope.months) >= 2
        ):
            wanted = set(period_scope.months)
            aggregate_expression = ", ".join(
                f"SUM({query_name}) AS period_{period.replace('-', '_')}"
                for period, query_name in metric_scope.columns
                if period in wanted
            )
            aggregate_alias = (
                f"period_{period_scope.months[-1].replace('-', '_')}"
            )
            resolved.extend(("measure", "period_comparison"))
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
            elif _is_distribution_question(question):
                dimension = _distribution_dimension_column(question, columns)
                if dimension is None:
                    if re.search(r"지역|region|area", question, re.IGNORECASE):
                        missing.append("지역")
                    elif re.search(r"거래처|customer|account", question, re.IGNORECASE):
                        missing.append("거래처")
                    else:
                        missing.append("channel_dimension_missing")
                else:
                    dimension_query = str(dimension.get("query_name") or "")
                    select_prefix = f"{dimension_query}, "
                    group_suffix = (
                        f" GROUP BY {dimension_query} ORDER BY {aggregate_alias} DESC"
                    )
                    if re.search(r"지역|region|area", question, re.IGNORECASE):
                        resolved.append("region")
                    elif re.search(r"거래처|customer|account", question, re.IGNORECASE):
                        resolved.append("customer")
                    else:
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
                    filters.append(
                        f"{product_query} LIKE "
                        f"{_sql_like_contains_literal(product_subject)} ESCAPE '\\'"
                    )
                    if not select_prefix:
                        select_prefix = f"{product_query}, "
                    if not group_suffix:
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
        aggregate_select = aggregate_expression
        if " LIMIT " in group_suffix.upper():
            aggregate_match = re.fullmatch(
                r"(?P<expression>SUM\(.+\))\s+AS\s+[A-Za-z_][A-Za-z0-9_]*",
                aggregate_expression,
                re.IGNORECASE,
            )
            if aggregate_match is not None:
                aggregate_select += (
                    f", SUM({aggregate_match.group('expression')}) OVER () "
                    "AS scope_total_value"
                )
        return DeterministicPlanResolution(
            {
                "logical_name": str(schema.get("logical_name") or ""),
                "sql": (
                    f"SELECT {select_prefix}{aggregate_select}, COUNT(*) AS applied_rows "
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
    if match is not None:
        return min(max(int(match.group(1) or match.group(2)), 1), 100)
    if re.search(
        r"(?:가장|최대|최고).{0,20}(?:매출|비중|브랜드|제품)|"
        r"(?:매출|비중).{0,12}(?:높|최대|최고)|(?:주요|순위)",
        question,
        re.IGNORECASE,
    ):
        try:
            return min(
                max(int(os.getenv("JW_CHAT_FILE_SQL_DEFAULT_TOP_N", "5")), 1),
                100,
            )
        except ValueError:
            return 5
    return None


def _is_channel_distribution_question(question: str) -> bool:
    return bool(
        re.search(
            r"(?:채널|거래처|customer|account).{0,12}(?:별|분포)|"
            r"by\s+(?:channel|customer|account)",
            question,
            re.IGNORECASE,
        )
    )


def _is_distribution_question(question: str) -> bool:
    return bool(
        re.search(
            r"(?:채널|거래처|지역|channel|customer|account|region|area).{0,12}(?:별|분포)|"
            r"by\s+(?:channel|customer|account|region|area)",
            question,
            re.IGNORECASE,
        )
    )


def _is_yoy_question(question: str) -> bool:
    return bool(
        re.search(
            r"전년\s*동월|동월\s*대비|yoy|year\s*over\s*year|성장률",
            question,
            re.IGNORECASE,
        )
    )


def _is_explicit_period_comparison(question: str) -> bool:
    return (len(month_keys(question)) >= 2 or _is_yoy_question(question)) and bool(
        re.search(r"비교|대비|증감|yoy|year\s*over\s*year", question, re.IGNORECASE)
        or _is_yoy_question(question)
    )


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


def _semantic_type_for_column(column: Mapping[str, Any]) -> str:
    source_name = str(column.get("source_name") or "")
    if re.search(
        r"(?:^|\b)(?:channel|채널|chc\s*\d+)(?:\b|$)",
        source_name,
        re.IGNORECASE,
    ):
        return "channel"
    if re.search(
        r"(?:atc\s*4|therapeutic|치료군|약효|class)",
        source_name,
        re.IGNORECASE,
    ):
        return "therapeutic_class"
    if re.search(
        r"(?:^|\b)(?:mfr|manufacturer|company|brand|product|customer|account|"
        r"제조사|업체|브랜드|제품|거래처)(?:\b|$)",
        source_name,
        re.IGNORECASE,
    ):
        return "entity"
    if (
        _is_amount_column(source_name)
        or _is_average_column(source_name)
        or _is_quantity_column(source_name)
    ):
        return "metric"
    if month_keys(source_name):
        return "period"
    return "unknown"


def _measure_basis_for_column(
    column: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str:
    source_name = " ".join(
        str(column.get("source_name") or "").casefold().replace("-", " ").split()
    )
    context = " ".join(
        " ".join(
            str(schema.get(key) or "").casefold().replace("-", " ").split()
        )
        for key in ("file_name", "sheet_name", "logical_name")
    )
    if any(token in context for token in ("sell out", "sellout", "chso")):
        return "sell_out"
    if any(
        token in source_name
        for token in ("sell out", "sellout", "values lc so price")
    ):
        return "sell_out"
    if any(token in context for token in ("sell in", "sellin", "chsi")):
        return "sell_in"
    if any(
        token in source_name
        for token in ("sell in", "sellin", "values lc si price")
    ):
        return "sell_in"
    return "unknown"


def _semantic_column_profile(
    column: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    semantic_type = _semantic_type_for_column(column)
    source_name = str(column.get("source_name") or "")
    if semantic_type in {"entity", "channel", "therapeutic_class", "period"}:
        aggregation_rule = "group_by"
    elif _is_average_column(source_name):
        aggregation_rule = "avg"
    elif semantic_type == "metric":
        aggregation_rule = "sum"
    else:
        aggregation_rule = "none"
    unit = (
        "원"
        if semantic_type == "metric"
        and (_is_amount_column(source_name) or _is_average_column(source_name))
        else ""
    )
    valid_dimensions = (
        ("entity", "channel", "therapeutic_class", "period")
        if semantic_type == "metric"
        else ()
    )
    return {
        **dict(column),
        "semantic_type": semantic_type,
        "measure_basis": _measure_basis_for_column(column, schema),
        "unit": unit,
        "aggregation_rule": aggregation_rule,
        "valid_dimensions": valid_dimensions,
    }


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


def _channel_dimension_column(
    columns: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    candidates = tuple(
        item
        for item in columns
        if re.search(
            r"(?:^|\b)channel(?:\b|$)|채널|^CHC\s*\d+$",
            str(item.get("source_name") or ""),
            re.IGNORECASE,
        )
    )
    if not candidates:
        return None

    def specificity(item: Mapping[str, Any]) -> tuple[int, int]:
        source_name = str(item.get("source_name") or "")
        match = re.search(r"\bCHC\s*(\d+)\b", source_name, re.IGNORECASE)
        if match:
            return (2, int(match.group(1)))
        return (1, 0)

    return max(candidates, key=specificity)


def _customer_dimension_column(
    columns: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return _find_column(
        columns,
        r"(?:^|\b)(?:customer|account)(?:\b|$)|거래처",
    )


def _distribution_dimension_column(
    question: str,
    columns: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if re.search(r"지역|region|area", question, re.IGNORECASE):
        return _find_column(
            columns,
            r"(?:^|\b)(?:region|area)(?:\b|$)|지역",
        )
    if re.search(r"채널|channel", question, re.IGNORECASE):
        return _channel_dimension_column(columns)
    if re.search(r"거래처|customer|account", question, re.IGNORECASE):
        customer = _customer_dimension_column(columns)
        return customer or _channel_dimension_column(columns)
    return _channel_dimension_column(columns)


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


def _excluded_aggregate_row_count(
    source_row_count: int | None,
    applied_row_count: float,
    sql: str,
) -> int | None:
    """Return rows excluded by the structural predicate when the count is auditable."""

    if source_row_count is None:
        return None
    if not _has_only_aggregate_row_exclusion(sql):
        return None
    where_match = re.search(r"\bWHERE\b(?P<where>.+?)(?:\bGROUP\s+BY\b|$)", sql, re.IGNORECASE)
    assert where_match is not None
    try:
        applied = int(applied_row_count)
    except (TypeError, ValueError, OverflowError):
        return None
    if applied < 0 or source_row_count < applied:
        return None
    return source_row_count - applied


def _has_only_aggregate_row_exclusion(sql: str) -> bool:
    """Return whether the WHERE clause contains only the structural total filter."""

    where_match = re.search(r"\bWHERE\b(?P<where>.+?)(?:\bGROUP\s+BY\b|$)", sql, re.IGNORECASE)
    if where_match is None:
        return False
    remaining = _TOTAL_ROW_CLAUSE_RE.sub("", where_match.group("where"), count=1)
    return _TOTAL_ROW_CLAUSE_RE.search(where_match.group("where")) is not None and not re.sub(
        r"[\s()]+", "", remaining
    )


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


def _metric_scope_for_family(
    columns: Sequence[Mapping[str, Any]],
    family: str,
) -> MetricScope | None:
    period_columns = _metric_period_columns(columns, family)
    if not period_columns:
        return None
    return MetricScope(
        family=family,
        label=_METRIC_LABELS.get(family, family),
        defaulted=False,
        columns=period_columns,
    )


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
        requested_months = sorted(explicit_months)
        if _is_yoy_question(question) and len(requested_months) == 1:
            year, month = requested_months[0].split("-", 1)
            requested_months.insert(0, f"{int(year) - 1:04d}-{month}")
        requested.extend(requested_months)
        labels.extend(requested_months)
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

    if not requested and _is_yoy_question(question):
        latest = months[-1]
        year, month = latest.split("-", 1)
        previous = f"{int(year) - 1:04d}-{month}"
        requested.extend((previous, latest))
        labels.extend((previous, latest))

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
    if re.search(r"(?<![A-Za-z0-9])JW(?=\s*제품(?:명)?(?:\s|$))", question, re.IGNORECASE):
        return "JW중외제약"
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
    r"(?:\s*계열)?"
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
        "대비", "계열", "알려줘", "보여줘", "확인", "정리", "요약",
        "unit", "units", "values", "lc", "price",
    }
)
_SUBJECT_STOPWORD_PARTICLES: Final[tuple[str, ...]] = (
    "에서",
    "에는",
    "으로",
    "의",
    "은",
    "는",
    "이",
    "가",
)


def _is_subject_stopword(subject: str) -> bool:
    normalized = subject.casefold()
    if normalized in _SUBJECT_STOPWORDS:
        return True
    return any(
        normalized == f"{stopword}{particle}"
        for stopword in _SUBJECT_STOPWORDS
        for particle in _SUBJECT_STOPWORD_PARTICLES
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
    if re.search(
        r"(?:제품|브랜드|제조사|업체)\s*별",
        question,
        re.IGNORECASE,
    ):
        return ""
    if _find_column(columns, r"(?:^|\b)product(?:\s+name)?(?:\b|$)|제품(?:명)?") is None:
        return ""
    candidates: list[str] = []
    trend_match = re.match(
        r"\s*(?P<subject>[가-힣A-Za-z0-9][가-힣A-Za-z0-9._-]{1,})"
        r"(?:의|은|는|이|가)?\s*(?=월별\s*(?:추이|시계열))",
        question,
        re.IGNORECASE,
    )
    if trend_match is not None:
        subject = trend_match.group("subject").strip()
        if subject and not _is_subject_stopword(subject):
            candidates.append(subject)
    if _is_distribution_question(question):
        match = re.match(
            r"\s*(?P<subject>[가-힣A-Za-z0-9][가-힣A-Za-z0-9._-]{1,})"
            r"(?:의|은|는|이|가)?\s*"
            r"(?:\d{2,4}\s*년\s*)?(?:\d{1,2}\s*월\s*)?"
            r"(?=(?:채널|거래처|지역|channel|customer|account|region|area))",
            question,
            re.IGNORECASE,
        )
        if match is not None:
            subject = match.group("subject").strip()
            if subject and not _is_subject_stopword(subject):
                candidates.append(subject)
    yoy_match = re.search(
        r"(?:^|[\s,])(?P<subject>[가-힣A-Za-z0-9][가-힣A-Za-z0-9._-]{1,})"
        r"(?:의|은|는|이|가)?\s*전년\s*동월\s*대비.*?성장률",
        question,
        re.IGNORECASE,
    )
    if yoy_match is not None:
        subject = yoy_match.group("subject").strip()
        if subject and not _is_subject_stopword(subject):
            candidates.append(subject)
    for match in _SUBJECT_MEASURE_RE.finditer(question):
        subject = match.group("subject").strip()
        if not subject or _is_subject_stopword(subject):
            continue
        if subject.endswith("별"):
            continue
        if re.search(r"\d{1,2}\s*월", subject):
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
    if known_names:
        return ""
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


def _sql_like_contains_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return _sql_literal(f"%{escaped}%")


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


def _enrich_generated_schemas(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
    conversation_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Attach bounded, observed cell examples to schemas used by SQL generation."""

    enriched: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for schema in schemas:
        logical_name = str(schema.get("logical_name") or "").strip()
        selected = _generated_sample_columns(question, schema)
        if not logical_name or not selected:
            enriched.append(dict(schema))
            continue
        query_names = [str(item.get("query_name") or "") for item in selected]
        sql = (
            "SELECT "
            + ", ".join(query_names)
            + f" FROM data LIMIT {_generated_sql_sample_row_limit()}"
        )
        try:
            result = _run_query(conversation_id, logical_name, sql)
            result_columns = result.get("columns")
            result_rows = result.get("rows")
            if not isinstance(result_columns, list) or not isinstance(result_rows, list):
                raise TypeError("file SQL sample response is malformed")
            rows = [row for row in result_rows if isinstance(row, list)]
            observed = _observed_column_samples(result_columns, rows)
            columns = [
                {
                    **dict(item),
                    **observed.get(str(item.get("query_name") or ""), {}),
                }
                for item in _schema_columns(schema)
            ]
            enriched.append({**dict(schema), "columns": columns})
            details.append(
                {
                    "logical_name": logical_name,
                    "query": sql,
                    "columns": [str(value) for value in result_columns],
                    "rows": rows,
                    "row_count": len(rows),
                    "status": "ok",
                }
            )
        except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
            logger.warning(
                "file SQL schema sample unavailable logical_name=%s reason=%s",
                logical_name,
                _failure_reason(exc),
            )
            enriched.append(dict(schema))
            details.append(
                {
                    "logical_name": logical_name,
                    "query": sql,
                    "columns": query_names,
                    "rows": [],
                    "row_count": 0,
                    "status": "failed",
                    "failure_reason": _failure_reason(exc),
                }
            )
    return tuple(enriched), tuple(details)


def _generated_sample_columns(
    question: str,
    schema: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    compact = _compact_schema(question, schema)
    columns = list(_schema_columns(compact))
    if not columns:
        return ()
    tokens = _question_tokens(question)
    question_months = month_keys(question)
    matched = [
        item
        for item in columns
        if _column_matches_question(item, tokens, question_months)
    ]
    identity = columns[: min(_identity_column_count(), len(columns))]
    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in [*matched, *identity, *columns]:
        query_name = str(item.get("query_name") or "").strip()
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", query_name)
            or query_name in seen
        ):
            continue
        seen.add(query_name)
        selected.append(item)
        if len(selected) >= _generated_sql_sample_column_limit():
            break
    return tuple(selected)


def _observed_column_samples(
    columns: Sequence[Any],
    rows: Sequence[Sequence[Any]],
) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for index, raw_name in enumerate(columns):
        name = str(raw_name)
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        samples: list[Any] = []
        for value in values:
            normalized = _bounded_sample_value(value)
            if normalized not in samples:
                samples.append(normalized)
        observed[name] = {
            "observed_value_type": _observed_value_type(values),
            "sample_values": samples[: _generated_sql_sample_row_limit()],
        }
    return observed


def _observed_value_type(values: Sequence[Any]) -> str:
    kinds = {
        "boolean"
        if isinstance(value, bool)
        else "integer"
        if isinstance(value, int)
        else "number"
        if isinstance(value, float)
        else "text"
        for value in values
    }
    if not kinds:
        return "null"
    if kinds <= {"integer", "number"}:
        return "number" if "number" in kinds else "integer"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _bounded_sample_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text = str(value)
    limit = _generated_sql_sample_value_chars()
    return text if len(text) <= limit else text[:limit] + "..."


def _generate_select(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
    validation_feedback: str = "",
    conversation_id: str = "",
) -> dict[str, Any] | None:
    """Ask the planner for SQL only; validation and execution remain code-owned."""

    token = resolve_planner_genos_token()
    if not token:
        raise RuntimeError("planner token is unavailable")
    generation_schemas = tuple(dict(schema) for schema in schemas)
    schema_samples: tuple[dict[str, Any], ...] = ()
    if conversation_id:
        generation_schemas, schema_samples = _enrich_generated_schemas(
            question,
            schemas,
            conversation_id,
        )
    compact_schemas = [
        _compact_schema(question, schema) for schema in generation_schemas
    ]
    request: dict[str, Any] = {
        "question": question,
        "uploaded_file_schemas": compact_schemas,
    }
    if validation_feedback:
        request["validation_feedback"] = validation_feedback
        request["instruction"] = (
            "Regenerate once and correct only the validation failure. "
            "Do not invent tables, columns, or values."
        )
    response = requests.post(
        f"{resolve_planner_genos_base_url().rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [
                {"role": "system", "content": _planner_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(request, ensure_ascii=False),
                },
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": _planner_max_tokens(),
        },
        timeout=_planner_timeout(),
    )
    response.raise_for_status()
    payload = response.json()
    content = _message_content(payload)
    parsed = _json_object(content)
    logical_name = str(parsed.get("logical_name") or "").strip()
    sql = str(parsed.get("sql") or "").strip()
    if not logical_name and not sql:
        return None
    return {
        "logical_name": logical_name,
        "sql": sql,
        "_schema_samples": list(schema_samples),
    }


def _resolve_generated_select(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
    conversation_id: str = "",
) -> GeneratedPlanResolution:
    feedback = ""
    last_reason = "empty_plan"
    schema_samples: tuple[dict[str, Any], ...] = ()
    for attempt in range(1, 3):
        try:
            candidate = _generate_select(
                question,
                schemas,
                feedback,
                conversation_id,
            )
        except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
            logger.warning(
                "file SQL generation unavailable attempt=%s reason=%s",
                attempt,
                _failure_reason(exc),
            )
            return GeneratedPlanResolution(
                None,
                attempts=attempt,
                failure_reason=_failure_reason(exc),
                schema_samples=schema_samples,
            )
        if candidate is None:
            return GeneratedPlanResolution(
                None,
                attempts=attempt,
                failure_reason="empty_plan",
                schema_samples=schema_samples,
            )
        raw_samples = candidate.get("_schema_samples")
        if isinstance(raw_samples, (list, tuple)):
            schema_samples = tuple(
                dict(item) for item in raw_samples if isinstance(item, dict)
            )
        candidate = {
            "logical_name": str(candidate.get("logical_name") or ""),
            "sql": str(candidate.get("sql") or ""),
        }
        try:
            plan = _validate_generated_select(candidate, schemas)
        except GeneratedSqlValidationError as exc:
            last_reason = exc.reason
            feedback = exc.reason
            logger.warning(
                "file SQL generated plan rejected attempt=%s reason=%s",
                attempt,
                exc.reason,
            )
            continue
        return GeneratedPlanResolution(
            plan,
            attempts=attempt,
            schema_samples=schema_samples,
        )
    return GeneratedPlanResolution(
        None,
        attempts=2,
        failure_reason=last_reason,
        schema_samples=schema_samples,
    )


_GENERATED_SQL_KEYWORDS: Final = frozenset(
    {
        "select", "from", "where", "and", "or", "not", "null", "is", "in",
        "like", "escape", "between", "case", "when", "then", "else", "end",
        "as", "distinct", "group", "by", "having", "order", "asc", "desc",
        "limit", "offset", "collate", "nocase", "true", "false",
    }
)
_GENERATED_SQL_FUNCTIONS: Final = frozenset(
    {
        "sum", "avg", "count", "min", "max", "coalesce", "round", "abs",
        "nullif", "trim", "lower", "upper",
    }
)
_GENERATED_SQL_FORBIDDEN: Final = frozenset(
    {
        "insert", "update", "delete", "replace", "drop", "alter", "create",
        "attach", "detach", "pragma", "vacuum", "reindex", "trigger", "view",
        "virtual", "load_extension", "recursive", "returning",
    }
)


def _validate_generated_select(
    candidate: Mapping[str, Any],
    schemas: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    logical_name = str(candidate.get("logical_name") or "").strip()
    schema = next(
        (
            item
            for item in schemas
            if str(item.get("logical_name") or "").strip() == logical_name
        ),
        None,
    )
    if schema is None:
        raise GeneratedSqlValidationError("unknown_logical_name")
    sql = str(candidate.get("sql") or "").strip()
    if not sql:
        raise GeneratedSqlValidationError("empty_sql")
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    masked = _mask_generated_sql_literals(sql)
    if ";" in masked:
        raise GeneratedSqlValidationError("multiple_statements")
    if "--" in masked or "/*" in masked or "*/" in masked:
        raise GeneratedSqlValidationError("sql_comments")
    if not re.match(r"^\s*SELECT\b", masked, re.IGNORECASE):
        raise GeneratedSqlValidationError("unsafe_statement")
    lowered = masked.casefold()
    if any(
        re.search(rf"\b{re.escape(keyword)}\b", lowered)
        for keyword in _GENERATED_SQL_FORBIDDEN
    ):
        raise GeneratedSqlValidationError("unsafe_statement")
    if re.search(r"\bsqlite_", lowered) or re.search(r"[`\"\[\]]", masked):
        raise GeneratedSqlValidationError("unsafe_identifier")
    table_matches = re.findall(
        r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)",
        lowered,
    )
    if not table_matches or any(table != "data" for table in table_matches):
        raise GeneratedSqlValidationError("unknown_table")
    if re.search(r"\b(?:from|join)\s*\(", lowered):
        raise GeneratedSqlValidationError("subquery_not_allowed")

    allowed_columns = {
        str(item.get("query_name") or "").casefold()
        for item in _schema_columns(schema)
        if str(item.get("query_name") or "").strip()
    }
    aliases = {
        match.casefold()
        for match in re.findall(r"\bas\s+([a-z_][a-z0-9_]*)", lowered)
    }
    allowed_identifiers = {
        "data",
        *allowed_columns,
        *aliases,
        *_GENERATED_SQL_KEYWORDS,
        *_GENERATED_SQL_FUNCTIONS,
    }
    identifiers = re.findall(r"\b[a-z_][a-z0-9_]*\b", lowered)
    unknown = tuple(
        dict.fromkeys(token for token in identifiers if token not in allowed_identifiers)
    )
    if unknown:
        raise GeneratedSqlValidationError("unknown_column")
    projected = re.sub(r"\bcount\s*\(\s*\*\s*\)", "", lowered)
    if "*" in projected:
        raise GeneratedSqlValidationError("wildcard_projection")
    if (
        re.search(r"\b(?:sum|avg|count|min|max)\s*\(", lowered)
        and not _has_aggregate_contract(sql)
    ):
        raise GeneratedSqlValidationError("missing_aggregate_contract")

    has_aggregate = bool(
        re.search(r"\b(?:sum|avg|count|min|max)\s*\(", lowered)
    )
    is_scalar_aggregate = (
        has_aggregate
        and not re.search(r"\bgroup\s+by\b", lowered)
        and not re.search(r"\bover\s*\(", lowered)
    )
    if not is_scalar_aggregate and not re.search(r"\border\s+by\b", lowered):
        raise GeneratedSqlValidationError("missing_order_by")

    cap = _generated_sql_row_limit()
    limit_matches = tuple(re.finditer(r"\blimit\s+(\d+)\b", lowered))
    if len(limit_matches) > 1:
        raise GeneratedSqlValidationError("invalid_limit")
    if limit_matches:
        match = limit_matches[0]
        if lowered[match.end() :].strip():
            raise GeneratedSqlValidationError("invalid_limit")
        current = int(match.group(1))
        if current > cap:
            sql = f"{sql[:match.start()]}LIMIT {cap}"
    else:
        sql = f"{sql} LIMIT {cap}"
    return {"logical_name": logical_name, "sql": sql}


def _mask_generated_sql_literals(sql: str) -> str:
    masked: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char != "'":
            masked.append(char)
            index += 1
            continue
        masked.append(" ")
        index += 1
        while index < len(sql):
            if sql[index] == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                masked.extend((" ", " "))
                index += 2
                continue
            if sql[index] == "'":
                masked.append(" ")
                index += 1
                break
            masked.append(" ")
            index += 1
        else:
            raise GeneratedSqlValidationError("unterminated_literal")
    return "".join(masked)


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
        raise TypeError("file SQL query response must be an object")
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
    columns = [_semantic_column_profile(item, schema) for item in columns]
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
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_QUANTITY_TERMS",
        DEFAULT_QUANTITY_TERMS,
    ) or (
        _UNIT_COLUMN_RE.search(question)
        and not re.search(r"unit\s*price", question, re.IGNORECASE)
    ):
        return "quantity"
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_AMOUNT_QUESTION_TERMS",
        DEFAULT_AMOUNT_QUESTION_TERMS,
    ):
        return "amount"
    return None


def _requested_measure_families(question: str) -> tuple[str, ...]:
    """Return every explicitly requested measure instead of collapsing to one."""

    families: list[str] = []
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_AMOUNT_QUESTION_TERMS",
        DEFAULT_AMOUNT_QUESTION_TERMS,
    ):
        families.append(METRIC_AMOUNT)
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_QUANTITY_TERMS",
        DEFAULT_QUANTITY_TERMS,
    ) or (
        _UNIT_COLUMN_RE.search(question)
        and not re.search(r"unit\s*price", question, re.IGNORECASE)
    ):
        families.append(METRIC_QUANTITY)
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_AVERAGE_TERMS",
        DEFAULT_AVERAGE_TERMS,
    ):
        families.append(METRIC_AVERAGE)
    if _contains_configured_term(
        question,
        "JW_CHAT_FILE_SQL_COUNT_QUESTION_TERMS",
        DEFAULT_COUNT_QUESTION_TERMS,
    ):
        families.append("count")
    return tuple(dict.fromkeys(families))


def _selected_columns_match_requested_families(
    requested: Sequence[str],
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
    observed = {
        family
        for function, query_name in re.findall(
            r"\b(SUM|AVG)\s*\(\s*([A-Za-z][A-Za-z0-9_]*)\s*\)",
            sql,
            re.IGNORECASE,
        )
        if (family := _metric_family(source_names.get(query_name.casefold(), "")))
        and (
            (function.casefold() == "sum" and family != METRIC_AVERAGE)
            or (function.casefold() == "avg" and family == METRIC_AVERAGE)
        )
    }
    if "count" in requested and re.search(
        r"\bCOUNT\s*\([^)]*\)\s+(?:AS\s+)?(?!applied_rows\b)[A-Za-z][A-Za-z0-9_]*",
        sql,
        re.IGNORECASE,
    ):
        observed.add("count")
    return set(requested).issubset(observed)


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


def _ir_capabilities_timeout() -> float:
    return float(os.getenv("JW_CHAT_FILE_IR_CAPABILITIES_TIMEOUT_S", "15"))


def _planner_system_prompt() -> str:
    return os.getenv(
        "JW_CHAT_FILE_SQL_PLANNER_SYSTEM_PROMPT",
        (
            "You translate an uploaded-file question into one SQLite SELECT. "
            "Use exactly one supplied logical_name and query only its table alias data. "
            "Column query_name values (c1, c2, ...) are the only legal columns; "
            "source_name explains their meaning. observed_value_type and sample_values are "
            "bounded observations, not permission to invent unseen values. Compare categorical "
            "values with quoted string literals, and use SUM and AVG directly for numeric "
            "aggregates. Never use CAST because the scoped "
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


def _generated_sql_row_limit() -> int:
    return max(
        1,
        min(
            1_000,
            int(os.getenv("JW_CHAT_FILE_SQL_GENERATED_ROW_LIMIT", "100")),
        ),
    )


def _generated_sql_sample_column_limit() -> int:
    return max(
        1,
        min(
            32,
            int(os.getenv("JW_CHAT_FILE_SQL_SAMPLE_COLUMNS", "12")),
        ),
    )


def _generated_sql_sample_row_limit() -> int:
    return max(
        1,
        min(
            5,
            int(os.getenv("JW_CHAT_FILE_SQL_SAMPLE_ROWS", "3")),
        ),
    )


def _generated_sql_sample_value_chars() -> int:
    return max(
        16,
        min(
            500,
            int(os.getenv("JW_CHAT_FILE_SQL_SAMPLE_VALUE_CHARS", "120")),
        ),
    )


def _max_schema_tables() -> int:
    return max(1, int(os.getenv("JW_CHAT_FILE_SQL_MAX_TABLES", "4")))


def _max_schema_columns() -> int:
    return max(20, int(os.getenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "192")))


def _identity_column_count() -> int:
    return max(1, int(os.getenv("JW_CHAT_FILE_SQL_IDENTITY_COLUMNS", "24")))
