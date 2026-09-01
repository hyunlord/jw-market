from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Literal, assert_never

from pydantic import BaseModel, ConfigDict, model_validator


class RoutingMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENFORCE = "ENFORCE"


class DomainDecisionSource(StrEnum):
    PREFIX_RULE = "PREFIX_RULE"
    INTENT_OWNER = "INTENT_OWNER"
    METRIC_OWNER = "METRIC_OWNER"
    LLM = "LLM"
    UNRESOLVED = "UNRESOLVED"


class CapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    FIELD_NOT_EXPOSED = "FIELD_NOT_EXPOSED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNRESOLVED = "UNRESOLVED"


class ToolSelectionSource(StrEnum):
    LEGACY_RULE = "LEGACY_RULE"
    NEW_RULE = "NEW_RULE"
    DETERMINISTIC_SINGLETON = "DETERMINISTIC_SINGLETON"
    LLM = "LLM"
    NONE = "NONE"


class RouteOutcome(StrEnum):
    CALL = "CALL"
    NO_TOOL = "NO_TOOL"
    TYPED_STOP = "TYPED_STOP"


class RoutingV4ContractError(ValueError):
    """Raised when routing state violates the frozen v4 contract."""


def parse_routing_mode(raw: str | None) -> RoutingMode:
    normalized = str(raw or "").strip().upper()
    try:
        return RoutingMode(normalized)
    except ValueError:
        return RoutingMode.OFF


@dataclass(frozen=True, slots=True)
class RoutingTruthTableBehavior:
    mode: RoutingMode
    response_path: str
    legacy_force_contract_calls: bool
    new_router_enabled: bool
    new_provider_enabled: bool
    new_router_affects_response: bool
    new_router_executes_tools: bool


def routing_truth_table(
    mode: RoutingMode,
    *,
    force_contract_calls: bool,
) -> RoutingTruthTableBehavior:
    match mode:
        case RoutingMode.OFF:
            return RoutingTruthTableBehavior(
                mode=mode,
                response_path="legacy",
                legacy_force_contract_calls=force_contract_calls,
                new_router_enabled=False,
                new_provider_enabled=False,
                new_router_affects_response=False,
                new_router_executes_tools=False,
            )
        case RoutingMode.SHADOW | RoutingMode.ENFORCE:
            return RoutingTruthTableBehavior(
                mode=mode,
                response_path="legacy" if mode is RoutingMode.SHADOW else "new_router",
                legacy_force_contract_calls=force_contract_calls,
                new_router_enabled=True,
                new_provider_enabled=True,
                new_router_affects_response=mode is RoutingMode.ENFORCE,
                new_router_executes_tools=mode is RoutingMode.ENFORCE,
            )
        case unreachable:
            assert_never(unreachable)


class RoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_domain: str
    domain_decision_source: DomainDecisionSource
    capability_status: CapabilityStatus
    tool_selection_source: ToolSelectionSource
    route_outcome: RouteOutcome


class ProposedCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    normalized_args: dict[str, Any]


class ProposedRoutingSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    routing_mode: RoutingMode
    routing_decision: RoutingDecision
    proposed_calls: tuple[ProposedCall, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> ProposedRoutingSignature:
        has_calls = bool(self.proposed_calls)
        if (
            self.routing_decision.route_outcome is RouteOutcome.CALL
            and not has_calls
            and not _is_internal_legacy_call(self.routing_decision)
        ):
            raise RoutingV4ContractError("CALL routing outcome requires at least one proposed call")
        if self.routing_decision.route_outcome is not RouteOutcome.CALL and has_calls:
            raise RoutingV4ContractError("non-CALL routing outcome cannot contain proposed calls")
        return self


class ExecutedCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_ordinal: int
    parent_ordinal: int | None
    tool_name: str
    normalized_args: dict[str, Any]
    result_status: str


class ExecutedCallSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    routing_mode: RoutingMode
    routing_decision: RoutingDecision
    proposed_calls: tuple[ProposedCall, ...] = ()
    executed_calls: tuple[ExecutedCall, ...] = ()
    fallback_reason: str | None
    reason_code: str | None
    runtime_status: str

    @model_validator(mode="after")
    def validate_execution(self) -> ExecutedCallSignature:
        ordinals = tuple(call.call_ordinal for call in self.executed_calls)
        if ordinals != tuple(range(1, len(ordinals) + 1)):
            raise RoutingV4ContractError("executed call ordinals must be contiguous and start at one")
        for call in self.executed_calls:
            if call.parent_ordinal is not None and not 1 <= call.parent_ordinal < call.call_ordinal:
                raise RoutingV4ContractError("parent ordinal must reference an earlier executed call")
        proposed = Counter(_call_key(call.tool_name, call.normalized_args) for call in self.proposed_calls)
        executed = Counter(_call_key(call.tool_name, call.normalized_args) for call in self.executed_calls)
        if executed - proposed:
            raise RoutingV4ContractError("executed calls must be present in the proposed call set")
        if (
            self.routing_decision.route_outcome is RouteOutcome.CALL
            and not self.proposed_calls
            and not _is_internal_legacy_call(self.routing_decision)
        ):
            raise RoutingV4ContractError("CALL routing outcome requires a proposed call set")
        return self

    def as_proposed(self, *, routing_mode: RoutingMode | None = None) -> ProposedRoutingSignature:
        return ProposedRoutingSignature(
            routing_mode=routing_mode or self.routing_mode,
            routing_decision=self.routing_decision,
            proposed_calls=self.proposed_calls,
        )


class RoutingToolCallBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_ordinal: int
    tool_name: str
    timeout_s: float

    @model_validator(mode="after")
    def validate_values(self) -> RoutingToolCallBudget:
        if self.call_ordinal < 1:
            raise RoutingV4ContractError("tool call budget ordinal must be positive")
        if not self.tool_name:
            raise RoutingV4ContractError("tool call budget requires a tool name")
        if self.timeout_s <= 0:
            raise RoutingV4ContractError("tool call timeout must be positive")
        return self


class RoutingBudgetTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["external_tool_routing_v4_budget_v1"] = (
        "external_tool_routing_v4_budget_v1"
    )
    planner_initial_call_cap: int = 1
    planner_repair_call_cap: int = 1
    planner_calls_used: int | None
    planner_timeout_s: float
    planner_token_cap: int
    authority_tool_call_cap: int
    authority_tool_calls_planned: int
    authority_tool_calls_executed: int | None
    duplicate_canonical_call_cap: int = 0
    official_web_fallback_call_cap: int
    tool_call_timeouts: tuple[RoutingToolCallBudget, ...] = ()
    planner_latency_ms: float | None
    tool_execution_latency_ms: float | None
    routing_latency_ms: float | None

    @model_validator(mode="after")
    def validate_budget(self) -> RoutingBudgetTrace:
        if self.planner_initial_call_cap != 1 or self.planner_repair_call_cap != 1:
            raise RoutingV4ContractError("planner call budget must remain one plus one repair")
        planner_cap = self.planner_initial_call_cap + self.planner_repair_call_cap
        if self.planner_calls_used is not None and not 0 <= self.planner_calls_used <= planner_cap:
            raise RoutingV4ContractError("planner calls exceed the configured budget")
        if self.planner_timeout_s <= 0 or self.planner_token_cap <= 0:
            raise RoutingV4ContractError("planner timeout and token caps must be positive")
        if self.authority_tool_call_cap not in {0, 1, 5}:
            raise RoutingV4ContractError("authority tool call cap must be zero, one, or five")
        expected_authority_cap = (
            0
            if self.authority_tool_calls_planned == 0
            else 1
            if self.authority_tool_calls_planned == 1
            else 5
        )
        if self.authority_tool_call_cap != expected_authority_cap:
            raise RoutingV4ContractError(
                "authority tool call cap must match the planned call shape"
            )
        if not 0 <= self.authority_tool_calls_planned <= self.authority_tool_call_cap:
            raise RoutingV4ContractError("planned authority calls exceed the call cap")
        if (
            self.authority_tool_calls_executed is not None
            and not 0
            <= self.authority_tool_calls_executed
            <= self.authority_tool_calls_planned
        ):
            raise RoutingV4ContractError("executed authority calls exceed the proposed set")
        if self.duplicate_canonical_call_cap != 0:
            raise RoutingV4ContractError("duplicate canonical calls must remain forbidden")
        if self.official_web_fallback_call_cap not in {0, 1}:
            raise RoutingV4ContractError("official web fallback cap must be zero or one")
        if len(self.tool_call_timeouts) != self.authority_tool_calls_planned:
            raise RoutingV4ContractError("every proposed authority call requires a timeout budget")
        expected_ordinals = tuple(range(1, len(self.tool_call_timeouts) + 1))
        actual_ordinals = tuple(item.call_ordinal for item in self.tool_call_timeouts)
        if actual_ordinals != expected_ordinals:
            raise RoutingV4ContractError("tool timeout ordinals must be contiguous")
        for latency in (
            self.planner_latency_ms,
            self.tool_execution_latency_ms,
            self.routing_latency_ms,
        ):
            if latency is not None and latency < 0:
                raise RoutingV4ContractError("routing latencies cannot be negative")
        return self


def compare_proposed_routes(
    left: ProposedRoutingSignature,
    right: ProposedRoutingSignature,
) -> bool:
    return (
        left.routing_decision == right.routing_decision
        and sorted(_proposed_call_key(call) for call in left.proposed_calls)
        == sorted(_proposed_call_key(call) for call in right.proposed_calls)
    )


def proposed_call_key(call: ProposedCall) -> str:
    return _proposed_call_key(call)


def _proposed_call_key(call: ProposedCall) -> str:
    return _call_key(call.tool_name, call.normalized_args)


def _call_key(tool_name: str, normalized_args: dict[str, Any]) -> str:
    return json.dumps(
        {"tool_name": tool_name, "normalized_args": normalized_args},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_internal_legacy_call(decision: RoutingDecision) -> bool:
    return (
        decision.source_domain == "internal_mart"
        and decision.domain_decision_source is DomainDecisionSource.METRIC_OWNER
        and decision.capability_status is CapabilityStatus.SUPPORTED
        and decision.tool_selection_source is ToolSelectionSource.LEGACY_RULE
    )
