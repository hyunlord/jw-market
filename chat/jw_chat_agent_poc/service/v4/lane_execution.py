from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    PlannerOutput,
    SourceResult,
)
from jw_chat_agent_poc.service.v4.document_lane import document_record_lane
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet

# ``SOURCE_NAMES`` is the executor's fan-out list: the seven lanes that own a
# ``ToolQueries`` field. The uploaded-document lane has no such field - it is
# seeded from the session as a supplemental result - so iterating SOURCE_NAMES
# alone left it with no LaneExecutionRecord and therefore no source notice
# binding, which is what removed "업로드 문서" from 조회 제한 entirely.
#
# The ledger is a record of what happened, not a fan-out list, so it carries the
# document lane too. SOURCE_NAMES itself is deliberately left at seven: widening
# it would make ToolQueries.items() read a field that does not exist and would
# turn ``frozenset(SOURCE_NAMES) - {"document"}`` in the answer-surface selector
# from a no-op into a live subtraction.
DOCUMENT_LEDGER_SOURCES = ("document_rag", "document_sql")
LEDGER_SOURCE_NAMES: tuple[str, ...] = (
    *SOURCE_NAMES,
    *DOCUMENT_LEDGER_SOURCES,
)


def _ledger_sources(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
) -> tuple[str, ...]:
    """Return the lanes this turn is accountable for.

    Both document tools remain visible on every turn. Sessions without the
    corresponding upload type record ``unplanned``; active types derive their
    state from the document route accounting rather than from planner wording.
    """

    has_prior_turn = "prior_turn" in plan.answer_sources or any(
        result.source == "prior_turn" for result in results
    )
    return (*LEDGER_SOURCE_NAMES, "prior_turn") if has_prior_turn else LEDGER_SOURCE_NAMES


class LaneState(StrEnum):
    UNPLANNED = "unplanned"
    PLANNED_NOT_EXECUTED = "planned_not_executed"
    EXECUTED_SUCCESS = "executed_success"
    EXECUTED_SUCCESS_ZERO = "executed_success_zero"
    EXECUTED_FAILED = "executed_failed"
    EXECUTED_TIMEOUT = "executed_timeout"
    NO_DOCUMENT = "no_document"
    UNKNOWN = "unknown"


class SemanticFailureState(StrEnum):
    NOT_PLANNED = "NOT_PLANNED"
    PLANNED_NOT_EXECUTED = "PLANNED_NOT_EXECUTED"
    QUERY_FAILED = "QUERY_FAILED"
    ENTITY_UNRESOLVED = "ENTITY_UNRESOLVED"
    NO_ROWS_VERIFIED = "NO_ROWS_VERIFIED"
    VALUE_ZERO = "VALUE_ZERO"
    PARTIAL = "PARTIAL"


class QueryExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    status: str
    returned_count: int
    elapsed_ms: float
    failure_class: str
    reason_code: str | None = None


class LaneExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    planned: bool
    requested_calls: int
    call_count: int
    returned_count: int
    elapsed_ms: float
    state: LaneState
    failure_state: SemanticFailureState | None = None
    reason_code: str | None = None
    omitted_count: int = 0
    omitted_reason: str | None = None
    queries: tuple[QueryExecutionRecord, ...] = ()


def build_lane_execution_records(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    evidence_sets: Sequence[EvidenceSet] = (),
) -> dict[str, LaneExecutionRecord]:
    received_by_source = {
        evidence_set.source: evidence_set.coverage.records_received
        for evidence_set in evidence_sets
    }
    scope = plan.query_scope
    records: dict[str, LaneExecutionRecord] = {}
    for source in _ledger_sources(plan, results):
        if source in DOCUMENT_LEDGER_SOURCES:
            source_results = tuple(
                _document_lane_result(result, source)
                for result in results
                if result.source == "document"
                and _document_lane_planned(result, source)
            )
        else:
            source_results = tuple(result for result in results if result.source == source)
        requested = (
            int(scope.requested_calls.get(source, 0))
            if scope is not None
            else len(getattr(plan.tool_queries, source, ()))
        )
        selected = (
            int(scope.executed_calls.get(source, 0))
            if scope is not None
            else len(source_results)
        )
        planned = (
            bool(source_results)
            if source in DOCUMENT_LEDGER_SOURCES
            else requested > 0 or source in plan.answer_sources
        )
        returned = int(
            received_by_source.get(
                source,
                sum(
                    _returned_count(result.payload)
                    for result in source_results
                    if result.status == "ok"
                ),
            )
        )
        query_records = tuple(
            _query_execution_record(
                result,
                _returned_count(result.payload) if result.status == "ok" else 0,
            )
            for result in source_results
        )
        state, reason_code = _lane_state(source_results, returned)
        if not source_results:
            state = LaneState.PLANNED_NOT_EXECUTED if planned else LaneState.UNPLANNED
            recorded_reason = (
                str(scope.unexecuted_reasons.get(source) or "")
                if scope is not None
                else ""
            )
            reason_code = recorded_reason or ("not_executed" if planned else "not_planned")
        omitted_queries = (
            tuple(scope.omitted_queries.get(source, ())) if scope is not None else ()
        )
        omitted_count = max(len(omitted_queries), requested - selected, 0)
        omitted_reason = None
        if omitted_count:
            omitted_reason = "조회 상한" if omitted_queries else "사유 미기록"
        failure_state = _semantic_failure_state(
            state=state,
            reason_code=reason_code,
            results=source_results,
            omitted_count=omitted_count,
            returned_count=returned,
        )
        records[source] = LaneExecutionRecord(
            source=source,
            planned=planned,
            requested_calls=requested,
            call_count=len(source_results),
            returned_count=returned,
            elapsed_ms=sum(result.elapsed_ms for result in source_results),
            state=state,
            failure_state=failure_state,
            reason_code=reason_code,
            omitted_count=omitted_count,
            omitted_reason=omitted_reason,
            queries=query_records,
        )
    return records


def _semantic_failure_state(
    *,
    state: LaneState,
    reason_code: str | None,
    results: Sequence[SourceResult],
    omitted_count: int,
    returned_count: int,
) -> SemanticFailureState | None:
    failure_classes = {_result_failure_class(result) for result in results}
    if "entity_unresolved" in failure_classes or reason_code in {
        "disease_code_unresolved",
        "entity_unresolved",
    }:
        return SemanticFailureState.ENTITY_UNRESOLVED
    if "value_zero" in failure_classes or reason_code == "value_zero":
        return SemanticFailureState.VALUE_ZERO
    if returned_count > 0 and omitted_count > 0:
        return SemanticFailureState.PARTIAL
    match state:
        case LaneState.UNPLANNED:
            return SemanticFailureState.NOT_PLANNED
        case LaneState.PLANNED_NOT_EXECUTED:
            return SemanticFailureState.PLANNED_NOT_EXECUTED
        case LaneState.EXECUTED_SUCCESS_ZERO:
            return SemanticFailureState.NO_ROWS_VERIFIED
        case LaneState.EXECUTED_FAILED | LaneState.EXECUTED_TIMEOUT | LaneState.UNKNOWN:
            return SemanticFailureState.QUERY_FAILED
        case LaneState.EXECUTED_SUCCESS | LaneState.NO_DOCUMENT:
            return None


def source_notice_bindings_from_lane_execution(
    records: Mapping[str, LaneExecutionRecord],
) -> tuple[dict[str, object], ...]:
    bindings: list[dict[str, object]] = []
    ordered_sources = (*LEDGER_SOURCE_NAMES, *(
        source for source in records if source not in LEDGER_SOURCE_NAMES
    ))
    for source in ordered_sources:
        record = records.get(source)
        if record is None:
            continue
        if (
            record.state is not LaneState.EXECUTED_SUCCESS
            or source in DOCUMENT_LEDGER_SOURCES
        ):
            bindings.append(
                {
                    "record_id": None,
                    "notice": _lane_notice(record),
                    "reason_code": (
                        "executed_success"
                        if record.state is LaneState.EXECUTED_SUCCESS
                        else record.reason_code
                    ),
                    "exposure_layer": "F-scope",
                    "tool": source,
                    "returned_count": record.returned_count,
                    "failure_state": (
                        record.failure_state.value if record.failure_state else None
                    ),
                }
            )
        if record.omitted_count and record.call_count:
            bindings.append(
                {
                    "record_id": None,
                    "notice": (
                        f"{record.omitted_count}건은 실행되지 않았습니다"
                        f"({record.omitted_reason})."
                    ),
                    "reason_code": "partial_not_executed",
                    "exposure_layer": "F-scope",
                    "tool": source,
                    "omitted_count": record.omitted_count,
                    "omitted_reason": record.omitted_reason,
                    "failure_state": SemanticFailureState.PARTIAL.value,
                }
            )
    return tuple(bindings)


def _query_execution_record(result: SourceResult, returned_count: int) -> QueryExecutionRecord:
    failure_class = _result_failure_class(result)
    _, reason_code = _lane_state((result,), returned_count)
    return QueryExecutionRecord(
        query=result.query,
        status=result.status,
        returned_count=returned_count,
        elapsed_ms=result.elapsed_ms,
        failure_class=failure_class,
        reason_code=reason_code,
    )


def _document_lane_planned(result: SourceResult, lane: str) -> bool:
    if not isinstance(result.payload, Mapping):
        return False
    accounting = result.payload.get("route_accounting")
    if not isinstance(accounting, Mapping):
        return lane == "document_rag"
    route = accounting.get(lane)
    return isinstance(route, Mapping) and route.get("planned") is True


def _document_lane_result(result: SourceResult, lane: str) -> SourceResult:
    payload = dict(result.payload) if isinstance(result.payload, Mapping) else {}
    raw_records = payload.get("records")
    records = [
        record
        for record in raw_records if isinstance(record, Mapping)
        and document_record_lane(record) == lane
    ] if isinstance(raw_records, list) else []
    payload["records"] = records
    accounting = payload.get("route_accounting")
    route = accounting.get(lane) if isinstance(accounting, Mapping) else None
    execution_failure = (
        route.get("execution_failure") if isinstance(route, Mapping) else None
    )
    if isinstance(execution_failure, Mapping):
        failure_detail = dict(execution_failure)
        failure_class = str(failure_detail.get("failure_class") or "error").casefold()
        return result.model_copy(
            update={
                "payload": payload,
                "status": "timeout" if failure_class == "timeout" else "error",
                "failure_reason": (
                    "FILE_SQL_QUERY_TIMEOUT"
                    if failure_class == "timeout"
                    else "FILE_SQL_QUERY_FAILED"
                ),
                "failure_detail": failure_detail,
            }
        )
    status = "ok" if records else ("empty" if result.status == "ok" else result.status)
    return result.model_copy(update={"payload": payload, "status": status})


def _lane_state(
    results: Sequence[SourceResult],
    returned_count: int,
) -> tuple[LaneState, str | None]:
    if returned_count > 0:
        return LaneState.EXECUTED_SUCCESS, None
    if any(_provider_resource_limit(result) for result in results):
        return LaneState.EXECUTED_FAILED, "provider_resource_limit"
    failure_classes = {_result_failure_class(result) for result in results}
    statuses = {result.status for result in results}
    if "quota" in failure_classes:
        return LaneState.EXECUTED_FAILED, "quota_exhausted"
    if "timeout" in failure_classes or statuses & {"timeout", "deadline_exceeded"}:
        return LaneState.EXECUTED_TIMEOUT, "timeout"
    if "no_document" in failure_classes or "no_document" in statuses:
        return LaneState.NO_DOCUMENT, "no_document"
    if "0_results" in failure_classes or statuses <= {"ok", "empty"}:
        return LaneState.EXECUTED_SUCCESS_ZERO, "empty_result"
    if statuses & {"error", "upstream", "parse_error", "scope_limit", "quota"}:
        return LaneState.EXECUTED_FAILED, "query_failed"
    if results:
        return LaneState.UNKNOWN, "unknown"
    return LaneState.UNPLANNED, "not_planned"


def _result_failure_class(result: SourceResult) -> str:
    recorded = str(result.failure_detail.get("failure_class") or "").casefold()
    if recorded:
        return recorded
    if result.status == "quota" or result.failure_reason in {
        "RATE_LIMITED",
        "QUOTA_EXCEEDED",
    }:
        return "quota"
    if result.status in {"timeout", "deadline_exceeded"}:
        return "timeout"
    if result.status == "empty":
        return "0_results"
    if result.status == "no_document":
        return "no_document"
    return "none"


def _provider_resource_limit(result: SourceResult) -> bool:
    detail = result.failure_detail if isinstance(result.failure_detail, Mapping) else {}
    if str(detail.get("error_type") or "").casefold() == "provider_resource_limit":
        return True
    text = " ".join(
        str(value)
        for value in (
            detail.get("body_redacted"),
            result.failure_reason,
            result.notice,
        )
        if value
    ).casefold()
    return "resourcelimitexception" in text or (
        "no resources currently available" in text and "dsopenapi" in text
    )


def _lane_notice(record: LaneExecutionRecord) -> str:
    if record.state is LaneState.EXECUTED_SUCCESS:
        return f"조회·성공·{record.returned_count}건입니다."
    if record.state is LaneState.UNPLANNED:
        return "미계획입니다."
    if record.state is LaneState.PLANNED_NOT_EXECUTED:
        if record.reason_code == "disease_code_lookup_failed":
            return "질환 코드 조회가 실패해 통계 조회를 실행하지 않았습니다."
        if record.reason_code == "disease_code_unresolved":
            return "질환 코드를 확인하지 못해 통계 조회를 실행하지 않았습니다."
        return f"계획됐으나 실행되지 않았습니다({record.omitted_reason or '사유 미기록'})."
    if record.state is LaneState.EXECUTED_SUCCESS_ZERO:
        return "조회했으나 결과가 0건입니다."
    if record.state is LaneState.EXECUTED_TIMEOUT:
        return "응답 시간 초과로 완료되지 않았습니다."
    if record.state is LaneState.NO_DOCUMENT:
        return "세션에 연결된 문서를 찾지 못했습니다."
    if record.reason_code == "quota_exhausted":
        return "쿼터·한도 소진으로 완료되지 않았습니다."
    if record.reason_code == "provider_resource_limit":
        return "HIRA 원천 일시 장애 (호출량/서버 부하)"
    if record.state is LaneState.EXECUTED_FAILED:
        return "조회가 실패해 결과를 확인할 수 없습니다."
    return "실행 상태가 확인되지 않았습니다."


def _returned_count(payload: object) -> int:
    if isinstance(payload, Mapping):
        for key in ("records", "rows", "items", "studies"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        calls = payload.get("calls")
        if isinstance(calls, list):
            return sum(_returned_count(call) for call in calls)
        for key in ("render_data", "payload"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                nested_count = _returned_count(nested)
                if nested_count:
                    return nested_count
        for key in ("totalCount", "total_count", "count"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    if isinstance(payload, list):
        return len(payload)
    return 0
