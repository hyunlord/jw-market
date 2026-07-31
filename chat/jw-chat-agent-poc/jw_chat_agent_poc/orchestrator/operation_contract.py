from __future__ import annotations

from collections.abc import Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
import logging
import re
from typing import Any, Final, Literal, TypedDict

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.orchestrator.period_selection import (
    PeriodGrain,
    PeriodKey,
    PeriodRequestKind,
    PeriodResolution,
    PeriodSelection,
    canonical_observed_periods,
    period_selection_for_spec,
)
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryOperation,
    RequestQuerySpec,
    TimeGranularity,
)
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    ShadowGate,
    ShadowGateMode,
    emit_shadow_gate_observation,
    shadow_gate_mode,
)


class CoverageDecisionStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


class PeriodCoverageStatus(StrEnum):
    MATCH = "match"
    MISSING = "missing"
    EXTRA = "extra"
    UNVERIFIABLE = "unverifiable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, order=True)
class CoverageAxis:
    entity_id: str
    metric: str
    period: str


@dataclass(frozen=True, slots=True)
class OperationContract:
    required: tuple[CoverageAxis, ...]
    applicable: bool
    reason: str
    period_selection: PeriodSelection | None = None


@dataclass(frozen=True, slots=True)
class PeriodCoverage:
    status: PeriodCoverageStatus
    selection: PeriodSelection
    observed: tuple[PeriodKey, ...]
    missing: tuple[PeriodKey, ...]


@dataclass(frozen=True, slots=True)
class CoverageDecision:
    status: CoverageDecisionStatus
    reason: str
    required: tuple[CoverageAxis, ...]
    observed: tuple[CoverageAxis, ...]
    missing: tuple[CoverageAxis, ...]
    period_coverage: PeriodCoverage | None = None


class CoverageAxisObservation(TypedDict):
    entity_id: str
    metric: str
    period: str


class CoverageDecisionObservation(TypedDict):
    status: str
    reason: str
    required: list[CoverageAxisObservation] | Literal["N/A"]
    observed: list[CoverageAxisObservation] | Literal["N/A"]
    missing: list[CoverageAxisObservation] | Literal["N/A"]
    period_set: PeriodCoverageObservation | Literal["N/A"]


class PeriodCoverageObservation(TypedDict):
    status: str
    kind: str
    grain: str
    resolution: str
    expected_count: int
    observed_count: int
    missing_count: int
    expected_periods: list[str]
    observed_periods: list[str]
    missing_periods: list[str]
    anchor: str | None


logger = logging.getLogger(__name__)

_SUPPORTED_METRICS: Final[frozenset[str]] = frozenset(("sales", "share", "rank"))
_MEASURE_METRICS: Final[dict[str, str]] = {
    "market_share": "share",
    "rank": "rank",
    "ranking": "rank",
    "sales": "sales",
    "share": "share",
}
_CURRENT_QUERY_SPEC: ContextVar[RequestQuerySpec | None] = ContextVar(
    "operation_contract_query_spec",
    default=None,
)
_CURRENT_QUESTION_FINGERPRINT: ContextVar[str] = ContextVar(
    "operation_contract_question_fingerprint",
    default="",
)


def evaluate_plan_coverage(
    spec: RequestQuerySpec,
    plan: tuple[ToolCallPlan, ...],
) -> CoverageDecision:
    observed = tuple(sorted({axis for call in plan for axis in _plan_axes(call)}))
    contract = operation_contract(
        spec,
        observed_periods=tuple(axis.period for axis in observed),
    )
    if not contract.applicable:
        return _not_applicable_decision(contract.reason)
    return _coverage_decision(contract, observed)


def evaluate_actual_coverage(
    spec: RequestQuerySpec,
    calls: Collection[Mapping[str, Any]],
) -> CoverageDecision:
    observed = tuple(
        sorted(
            {
                axis
                for call in calls
                for axis in _actual_axes(call)
            }
        )
    )
    contract = operation_contract(
        spec,
        observed_periods=tuple(axis.period for axis in observed),
    )
    if not contract.applicable:
        return _not_applicable_decision(contract.reason)
    return _coverage_decision(contract, observed)


def evaluate_surface_coverage(
    spec: RequestQuerySpec,
    answer: str,
    calls: Collection[Mapping[str, Any]],
) -> CoverageDecision:
    actual = tuple(
        sorted({axis for call in calls for axis in _actual_axes(call)})
    )
    observed = _surface_axes(answer, actual)
    contract = operation_contract(
        spec,
        observed_periods=tuple(axis.period for axis in observed),
    )
    if not contract.applicable:
        return _not_applicable_decision(contract.reason)
    return _coverage_decision(contract, observed)


def coverage_decision_observation(
    decision: CoverageDecision,
) -> CoverageDecisionObservation:
    return {
        "status": decision.status.value,
        "reason": decision.reason,
        "required": _axes_observation(decision.required),
        "observed": _axes_observation(decision.observed),
        "missing": _axes_observation(decision.missing),
        "period_set": _period_coverage_observation(decision.period_coverage),
    }


def observe_plan_coverage(
    spec: RequestQuerySpec,
    plan: tuple[ToolCallPlan, ...],
    *,
    planner_kind: str,
    step: int,
) -> CoverageDecision:
    decision = evaluate_plan_coverage(spec, plan)
    if shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is not ShadowGateMode.OFF:
        logger.info(
            "operation_contract_plan_shadow step=%s planner=%s decision=%s",
            step,
            planner_kind,
            coverage_decision_observation(decision),
        )
    _emit_runtime_observations(
        decision,
        spec=spec,
        phase="plan",
        question_fingerprint=current_question_fingerprint(),
    )
    return decision


def observe_actual_coverage(
    spec: RequestQuerySpec,
    calls: Collection[Mapping[str, Any]],
    *,
    question_fingerprint: str = "",
) -> CoverageDecision:
    decision = evaluate_actual_coverage(spec, calls)
    if shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is not ShadowGateMode.OFF:
        logger.info(
            "operation_contract_actual_shadow decision=%s",
            coverage_decision_observation(decision),
        )
    _emit_runtime_observations(
        decision,
        spec=spec,
        phase="actual",
        question_fingerprint=question_fingerprint,
    )
    return decision


def observe_surface_coverage(
    spec: RequestQuerySpec,
    answer: str,
    calls: Collection[Mapping[str, Any]],
    *,
    question_fingerprint: str = "",
) -> CoverageDecision:
    decision = evaluate_surface_coverage(spec, answer, calls)
    if shadow_gate_mode(ShadowGate.OPERATION_CONTRACT) is not ShadowGateMode.OFF:
        logger.info(
            "operation_contract_surface_shadow decision=%s",
            coverage_decision_observation(decision),
        )
    _emit_runtime_observations(
        decision,
        spec=spec,
        phase="surface",
        question_fingerprint=question_fingerprint,
    )
    return decision


def _emit_runtime_observations(
    decision: CoverageDecision,
    *,
    spec: RequestQuerySpec,
    phase: str,
    question_fingerprint: str,
) -> None:
    period_count = (
        decision.period_coverage.selection.expected_count
        if decision.period_coverage is not None
        else len({axis.period for axis in decision.required})
    )
    emit_shadow_gate_observation(
        gate=ShadowGate.OPERATION_CONTRACT,
        phase=phase,
        status=decision.status.value,
        reason=decision.reason,
        required_count=len(decision.required),
        observed_count=len(decision.observed),
        missing_count=len(decision.missing),
        entity_count=len(spec.entities),
        metric_count=len(spec.metrics),
        period_count=period_count,
        question_fingerprint=question_fingerprint,
    )
    period = decision.period_coverage
    if period is None:
        return
    emit_shadow_gate_observation(
        gate=ShadowGate.PERIOD_SET,
        phase=phase,
        status=period.status.value,
        reason=decision.reason,
        required_count=period.selection.expected_count,
        observed_count=len(period.observed),
        missing_count=len(period.missing),
        entity_count=len(spec.entities),
        metric_count=len(spec.metrics),
        period_count=period.selection.expected_count,
        question_fingerprint=question_fingerprint,
    )


def set_current_query_spec(
    spec: RequestQuerySpec | None,
    *,
    question_fingerprint: str = "",
) -> None:
    _CURRENT_QUERY_SPEC.set(spec)
    _CURRENT_QUESTION_FINGERPRINT.set(question_fingerprint)


def current_query_spec() -> RequestQuerySpec | None:
    return _CURRENT_QUERY_SPEC.get()


def current_question_fingerprint() -> str:
    return _CURRENT_QUESTION_FINGERPRINT.get()


def clear_current_query_spec() -> None:
    _CURRENT_QUERY_SPEC.set(None)
    _CURRENT_QUESTION_FINGERPRINT.set("")


def _coverage_decision(
    contract: OperationContract,
    observed: tuple[CoverageAxis, ...],
) -> CoverageDecision:
    if contract.period_selection is not None:
        match contract.period_selection.resolution:
            case PeriodResolution.UNVERIFIABLE:
                return _unresolved_period_decision(
                    contract,
                    observed,
                    CoverageDecisionStatus.UNVERIFIABLE,
                    PeriodCoverageStatus.UNVERIFIABLE,
                )
            case PeriodResolution.INVALID:
                return _unresolved_period_decision(
                    contract,
                    observed,
                    CoverageDecisionStatus.INVALID,
                    PeriodCoverageStatus.INVALID,
                )
            case PeriodResolution.RESOLVED:
                pass
    normalized = _normalize_latest_axes(contract.required, observed)
    missing = tuple(axis for axis in contract.required if axis not in normalized)
    period_coverage = _period_coverage(contract.period_selection, normalized)
    return CoverageDecision(
        status=CoverageDecisionStatus.FAIL if missing else CoverageDecisionStatus.PASS,
        reason="missing_coverage" if missing else "complete_coverage",
        required=contract.required,
        observed=normalized,
        missing=missing,
        period_coverage=period_coverage,
    )


def _unresolved_period_decision(
    contract: OperationContract,
    observed: tuple[CoverageAxis, ...],
    status: CoverageDecisionStatus,
    period_status: PeriodCoverageStatus,
) -> CoverageDecision:
    selection = contract.period_selection
    assert selection is not None
    observed_periods = canonical_observed_periods(
        tuple(axis.period for axis in observed),
        selection.grain,
    )
    return CoverageDecision(
        status=status,
        reason=f"period_{selection.resolution.value}",
        required=(),
        observed=observed,
        missing=(),
        period_coverage=PeriodCoverage(
            status=period_status,
            selection=selection,
            observed=observed_periods,
            missing=(),
        ),
    )


def _period_coverage(
    selection: PeriodSelection | None,
    observed_axes: tuple[CoverageAxis, ...],
) -> PeriodCoverage | None:
    if selection is None:
        return None
    observed = canonical_observed_periods(
        tuple(axis.period for axis in observed_axes),
        selection.grain,
    )
    missing = tuple(period for period in selection.members if period not in observed)
    extra = tuple(period for period in observed if period not in selection.members)
    period_status = (
        PeriodCoverageStatus.MISSING
        if missing
        else PeriodCoverageStatus.EXTRA
        if extra
        else PeriodCoverageStatus.MATCH
    )
    return PeriodCoverage(
        status=period_status,
        selection=selection,
        observed=observed,
        missing=missing,
    )


def _not_applicable_decision(reason: str) -> CoverageDecision:
    return CoverageDecision(
        status=CoverageDecisionStatus.NOT_APPLICABLE,
        reason=reason,
        required=(),
        observed=(),
        missing=(),
    )


def operation_contract(
    spec: RequestQuerySpec,
    *,
    observed_periods: Collection[str] = (),
) -> OperationContract:
    if not spec.entities or not spec.metrics:
        return _not_applicable("extractor_failure")
    if any(entity.kind is not EntityKind.BRAND for entity in spec.entities):
        return _not_applicable("unsupported_entity_kind")
    if spec.source is not None:
        return _not_applicable("external_domain")
    if spec.requested_view is not None:
        return _not_applicable("general_view")
    period_selection = (
        period_selection_for_spec(spec, observed_periods)
        if len(spec.entities) == 1 and spec.metrics == ("sales",)
        else None
    )
    if period_selection is not None and not _stage1_supports_period_selection(
        spec,
        period_selection,
    ):
        period_selection = None
    match spec.operation:
        case QueryOperation.CURRENT_VALUE | QueryOperation.COMPARE_CURRENT:
            pass
        case QueryOperation.TIME_SERIES if period_selection is not None:
            pass
        case _:
            return _not_applicable("unsupported_operation")
    if not set(spec.metrics).issubset(_SUPPORTED_METRICS):
        return _not_applicable("unsupported_metric")
    if period_selection is None:
        if spec.window_count is not None or spec.granularity is not None:
            return _not_applicable("unsupported_operation")
        if (
            spec.start_period is not None
            and spec.end_period is not None
            and spec.start_period != spec.end_period
        ):
            return _not_applicable("period_range")
    if period_selection is not None:
        return _period_set_contract(spec, period_selection)
    period = spec.start_period or spec.end_period or "latest"
    required = tuple(
        CoverageAxis(entity.canonical_id, metric, period)
        for entity in spec.entities
        for metric in spec.metrics
    )
    if not required:
        return _not_applicable("extractor_failure")
    return OperationContract(required=required, applicable=True, reason="stage1_market_metric")


def _period_set_contract(
    spec: RequestQuerySpec,
    selection: PeriodSelection,
) -> OperationContract:
    required = tuple(
        CoverageAxis(spec.entities[0].canonical_id, "sales", period.value)
        for period in selection.members
    )
    return OperationContract(
        required=required,
        applicable=True,
        reason="stage1_period_set",
        period_selection=selection,
    )


def _stage1_supports_period_selection(
    spec: RequestQuerySpec,
    selection: PeriodSelection,
) -> bool:
    match selection.kind:
        case PeriodRequestKind.CLOSED_RANGE:
            return (
                selection.grain is PeriodGrain.MONTH
                or selection.resolution is not PeriodResolution.RESOLVED
            )
        case PeriodRequestKind.TRAILING_WINDOW:
            return (
                spec.granularity is TimeGranularity.QUARTER
                and spec.window_count == 4
            )
        case _:
            return False


def _not_applicable(reason: str) -> OperationContract:
    return OperationContract(required=(), applicable=False, reason=reason)


def _plan_axes(call: ToolCallPlan) -> tuple[CoverageAxis, ...]:
    if call.name == "compare_brands_series":
        period = str(call.arguments.get("period") or "latest").strip() or "latest"
        measure = str(call.arguments.get("measure") or "").strip().casefold()
        planned_metric = _MEASURE_METRICS.get(measure)
        metrics = (planned_metric,) if planned_metric is not None else ("sales", "share")
        brands = tuple(
            dict.fromkeys(
                str(call.arguments.get(key) or "").strip()
                for key in ("brand", "comparison_brand")
                if str(call.arguments.get(key) or "").strip()
            )
        )
        return tuple(
            CoverageAxis(brand, metric, period)
            for brand in brands
            for metric in metrics
        )
    brand = str(call.arguments.get("brand") or "").strip()
    if not brand:
        return ()
    metric = _plan_metric(call)
    if metric is None:
        return ()
    period = str(call.arguments.get("period") or "latest").strip() or "latest"
    return (CoverageAxis(brand, metric, period),)


def _plan_metric(call: ToolCallPlan) -> str | None:
    match call.name:
        case "get_metric" | "get_brand_series":
            measure = str(call.arguments.get("measure") or "").strip().casefold()
            return _MEASURE_METRICS.get(measure)
        case "get_brand_sales":
            return "sales"
        case "get_brand_share":
            return "share"
        case "get_top_brands":
            return "rank"
        case _:
            return None


def _actual_axes(call: Mapping[str, Any]) -> tuple[CoverageAxis, ...]:
    data = call.get("render_data")
    if not isinstance(data, Mapping) or _failed_actual_call(call, data):
        return ()
    period = str(data.get("period") or "latest").strip() or "latest"
    axes: set[CoverageAxis] = set()
    brand = _actual_brand(data)
    if brand:
        for metric in _actual_metrics(data):
            axes.add(CoverageAxis(brand, metric, period))
    segments = data.get("level_segments")
    if isinstance(segments, list | tuple):
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            segment_brand = str(segment.get("brand") or "").strip()
            if not segment_brand:
                continue
            if segment.get("rank") is not None:
                axes.add(CoverageAxis(segment_brand, "rank", period))
            if _has_value(segment, ("sales_krw", "sales_억원", "value_krw")):
                axes.add(CoverageAxis(segment_brand, "sales", period))
            if _has_value(segment, ("ms_recent_pct", "market_share", "share_pct")):
                axes.add(CoverageAxis(segment_brand, "share", period))
    return tuple(sorted(axes))


def _failed_actual_call(
    call: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    statuses = {
        str(call.get("status") or "").strip().casefold(),
        str(data.get("status") or "").strip().casefold(),
    }
    return bool(
        statuses
        & {
            "error",
            "failed",
            "no_data",
            "query_failed",
            "unsupported",
        }
    )


def _actual_brand(data: Mapping[str, Any]) -> str:
    brand = str(data.get("brand") or "").strip()
    if brand:
        return brand
    query_spec = data.get("query_spec")
    if not isinstance(query_spec, Mapping):
        return ""
    filters = query_spec.get("filters")
    if not isinstance(filters, Mapping):
        return ""
    return str(filters.get("brand") or "").strip()


def _actual_metrics(data: Mapping[str, Any]) -> tuple[str, ...]:
    metrics: set[str] = set()
    declared = str(data.get("metric") or data.get("measure") or "").strip().casefold()
    mapped = _MEASURE_METRICS.get(declared)
    if mapped is not None:
        metrics.add(mapped)
    if _has_value(data, ("sales_krw", "sales_억원", "value_krw")):
        metrics.add("sales")
    if _has_value(data, ("ms_recent_pct", "market_share", "share_pct")):
        metrics.add("share")
    if data.get("rank") is not None:
        metrics.add("rank")
    return tuple(sorted(metrics))


def _surface_axes(
    answer: str,
    actual: tuple[CoverageAxis, ...],
) -> tuple[CoverageAxis, ...]:
    actual_by_entity_metric = {
        (axis.entity_id, axis.metric): axis
        for axis in actual
    }
    observed: set[CoverageAxis] = set()
    lines = answer.splitlines()
    for index, line in enumerate(lines):
        header = _markdown_cells(line)
        if not header or index + 1 >= len(lines) or not _is_markdown_separator(lines[index + 1]):
            continue
        metric_columns = {
            column: metric
            for column, cell in enumerate(header)
            if (metric := _surface_metric(cell)) is not None
        }
        if not metric_columns:
            continue
        for row_line in lines[index + 2 :]:
            row = _markdown_cells(row_line)
            if not row:
                break
            entity = row[0].strip()
            for column, metric in metric_columns.items():
                if column >= len(row) or not _surface_value_present(row[column]):
                    continue
                axis = actual_by_entity_metric.get((entity, metric))
                if axis is not None:
                    observed.add(axis)
    for axis in actual:
        if axis in observed:
            continue
        if any(_line_surfaces_axis(line, axis) for line in lines):
            observed.add(axis)
    return tuple(sorted(observed))


def _markdown_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip() for cell in stripped[1:-1].split("|"))


def _is_markdown_separator(line: str) -> bool:
    cells = _markdown_cells(line)
    return bool(cells) and all(cell and set(cell) <= {"-", ":"} for cell in cells)


def _surface_metric(header: str) -> str | None:
    normalized = header.casefold()
    if "순위" in normalized or "rank" in normalized:
        return "rank"
    if "점유율" in normalized or normalized == "ms" or "share" in normalized:
        return "share"
    if "매출" in normalized or "sales" in normalized:
        return "sales"
    return None


def _surface_value_present(value: str) -> bool:
    missing = {
        "",
        "-",
        "—",
        "n/a",
        "na",
        "none",
        "null",
        "근거 불일치로 제외",
    }
    return value.strip().casefold() not in missing


def _line_surfaces_axis(line: str, axis: CoverageAxis) -> bool:
    if not _line_mentions_entity(line, axis.entity_id):
        return False
    match axis.metric:
        case "sales":
            return "매출" in line and ("억원" in line or "원" in line)
        case "share":
            return ("점유율" in line or "MS" in line.upper()) and "%" in line
        case "rank":
            return ("순위" in line or "랭킹" in line) and bool(re.search(r"\d+\s*위", line))
        case _:
            return False


def _line_mentions_entity(line: str, entity_id: str) -> bool:
    suffix = r"(?=$|[^0-9A-Za-z가-힣]|은|는|이|가|을|를|의|와|과|도|로|에서)"
    return re.search(
        rf"(?<![0-9A-Za-z가-힣]){re.escape(entity_id)}{suffix}",
        line,
    ) is not None


def _has_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    missing_strings = {"", "-", "—", "n/a", "na", "none", "null"}
    return any(
        value is not None
        and not (
            isinstance(value, str)
            and value.strip().casefold() in missing_strings
        )
        for key in keys
        if (value := data.get(key)) is not None
    )


def _normalize_latest_axes(
    required: tuple[CoverageAxis, ...],
    observed: tuple[CoverageAxis, ...],
) -> tuple[CoverageAxis, ...]:
    latest_pairs = {
        (axis.entity_id, axis.metric)
        for axis in required
        if axis.period == "latest"
    }
    return tuple(
        sorted(
            {
                CoverageAxis(axis.entity_id, axis.metric, "latest")
                if (axis.entity_id, axis.metric) in latest_pairs
                else axis
                for axis in observed
            }
        )
    )


def _axis_observation(axis: CoverageAxis) -> CoverageAxisObservation:
    return {
        "entity_id": axis.entity_id,
        "metric": axis.metric,
        "period": axis.period,
    }


def _axes_observation(
    axes: tuple[CoverageAxis, ...],
) -> list[CoverageAxisObservation] | Literal["N/A"]:
    if not axes:
        return "N/A"
    return [_axis_observation(axis) for axis in axes]


def _period_coverage_observation(
    coverage: PeriodCoverage | None,
) -> PeriodCoverageObservation | Literal["N/A"]:
    if coverage is None:
        return "N/A"
    selection = coverage.selection
    return {
        "status": coverage.status.value,
        "kind": selection.kind.value,
        "grain": selection.grain.value,
        "resolution": selection.resolution.value,
        "expected_count": selection.expected_count,
        "observed_count": len(coverage.observed),
        "missing_count": len(coverage.missing),
        "expected_periods": [period.value for period in selection.members],
        "observed_periods": [period.value for period in coverage.observed],
        "missing_periods": [period.value for period in coverage.missing],
        "anchor": selection.anchor.value if selection.anchor is not None else None,
    }
