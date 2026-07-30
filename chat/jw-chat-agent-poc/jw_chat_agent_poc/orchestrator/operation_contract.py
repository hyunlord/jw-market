from __future__ import annotations

from collections.abc import Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import Any, Final, Literal, TypedDict

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryOperation,
    RequestQuerySpec,
)


class CoverageDecisionStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


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


@dataclass(frozen=True, slots=True)
class CoverageDecision:
    status: CoverageDecisionStatus
    reason: str
    required: tuple[CoverageAxis, ...]
    observed: tuple[CoverageAxis, ...]
    missing: tuple[CoverageAxis, ...]


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


def evaluate_plan_coverage(
    spec: RequestQuerySpec,
    plan: tuple[ToolCallPlan, ...],
) -> CoverageDecision:
    contract = operation_contract(spec)
    if not contract.applicable:
        return _not_applicable_decision(contract.reason)
    observed = tuple(sorted({axis for call in plan for axis in _plan_axes(call)}))
    return _coverage_decision(contract, observed)


def evaluate_actual_coverage(
    spec: RequestQuerySpec,
    calls: Collection[Mapping[str, Any]],
) -> CoverageDecision:
    contract = operation_contract(spec)
    if not contract.applicable:
        return _not_applicable_decision(contract.reason)
    observed = tuple(
        sorted(
            {
                axis
                for call in calls
                for axis in _actual_axes(call)
            }
        )
    )
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
    }


def observe_plan_coverage(
    spec: RequestQuerySpec,
    plan: tuple[ToolCallPlan, ...],
    *,
    planner_kind: str,
    step: int,
) -> CoverageDecision:
    decision = evaluate_plan_coverage(spec, plan)
    logger.info(
        "operation_contract_plan_shadow step=%s planner=%s decision=%s",
        step,
        planner_kind,
        coverage_decision_observation(decision),
    )
    return decision


def observe_actual_coverage(
    spec: RequestQuerySpec,
    calls: Collection[Mapping[str, Any]],
) -> CoverageDecision:
    decision = evaluate_actual_coverage(spec, calls)
    logger.info(
        "operation_contract_actual_shadow decision=%s",
        coverage_decision_observation(decision),
    )
    return decision


def set_current_query_spec(spec: RequestQuerySpec | None) -> None:
    _CURRENT_QUERY_SPEC.set(spec)


def current_query_spec() -> RequestQuerySpec | None:
    return _CURRENT_QUERY_SPEC.get()


def clear_current_query_spec() -> None:
    _CURRENT_QUERY_SPEC.set(None)


def _coverage_decision(
    contract: OperationContract,
    observed: tuple[CoverageAxis, ...],
) -> CoverageDecision:
    normalized = _normalize_latest_axes(contract.required, observed)
    missing = tuple(axis for axis in contract.required if axis not in normalized)
    return CoverageDecision(
        status=CoverageDecisionStatus.FAIL if missing else CoverageDecisionStatus.PASS,
        reason="missing_coverage" if missing else "complete_coverage",
        required=contract.required,
        observed=normalized,
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


def operation_contract(spec: RequestQuerySpec) -> OperationContract:
    if not spec.entities or not spec.metrics:
        return _not_applicable("extractor_failure")
    if any(entity.kind is not EntityKind.BRAND for entity in spec.entities):
        return _not_applicable("unsupported_entity_kind")
    if spec.source is not None:
        return _not_applicable("external_domain")
    if spec.requested_view is not None:
        return _not_applicable("general_view")
    match spec.operation:
        case QueryOperation.CURRENT_VALUE | QueryOperation.COMPARE_CURRENT:
            pass
        case _:
            return _not_applicable("unsupported_operation")
    if not set(spec.metrics).issubset(_SUPPORTED_METRICS):
        return _not_applicable("unsupported_metric")
    if spec.window_count is not None or spec.granularity is not None:
        return _not_applicable("unsupported_operation")
    if (
        spec.start_period is not None
        and spec.end_period is not None
        and spec.start_period != spec.end_period
    ):
        return _not_applicable("period_range")
    period = spec.start_period or spec.end_period or "latest"
    required = tuple(
        CoverageAxis(entity.canonical_id, metric, period)
        for entity in spec.entities
        for metric in spec.metrics
    )
    if not required:
        return _not_applicable("extractor_failure")
    return OperationContract(required=required, applicable=True, reason="stage1_market_metric")


def _not_applicable(reason: str) -> OperationContract:
    return OperationContract(required=(), applicable=False, reason=reason)


def _plan_axes(call: ToolCallPlan) -> tuple[CoverageAxis, ...]:
    if call.name == "compare_brands_series":
        period = str(call.arguments.get("period") or "latest").strip() or "latest"
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
            for metric in ("sales", "share")
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
