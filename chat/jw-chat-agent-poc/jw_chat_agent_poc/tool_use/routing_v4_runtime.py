from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any

from jw_chat_agent_poc.common.timing import Timing
from jw_chat_agent_poc.tool_use.contracts import AgentResult
from jw_chat_agent_poc.tool_use.executor import AgentExecutor, CompletionPolicy
from jw_chat_agent_poc.tool_use.provider import ToolChoice, ToolChoiceProvider
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import (
    default_capability_matrix,
)
from jw_chat_agent_poc.tool_use.routing_v4_execution import (
    claim_evidence_bindings,
    normalize_execution_result,
    safe_execution_failure,
)
from jw_chat_agent_poc.tool_use.routing_v4_plan_support import RoutePlan, typed_message
from jw_chat_agent_poc.tool_use.routing_v4_planner import ExternalRoutePlanner
from jw_chat_agent_poc.tool_use.routing_v4_shadow import (
    ShadowTask,
    collect_with_budget,
    start_with_budget,
)
from jw_chat_agent_poc.tool_use.routing_v4_types import (
    CapabilityStatus,
    DomainDecisionSource,
    ExecutedCall,
    ExecutedCallSignature,
    ProposedRoutingSignature,
    RouteOutcome,
    RoutingV4ContractError,
    RoutingDecision,
    RoutingMode,
    ToolSelectionSource,
    parse_routing_mode,
)
from jw_chat_agent_poc.tool_use.specs import ToolSpec


LOGGER = logging.getLogger(__name__)
ROUTING_MODE_FLAG = "CHAT_TOOL_ROUTING_MODE"


@dataclass(frozen=True, slots=True)
class EnforcedRouteResult:
    result: AgentResult
    diagnostics: dict[str, Any]


class _StopAfterPlanProvider:
    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        return ToolChoice(None, {}, "canonical route exhausted", call_id=None)


def configured_routing_mode() -> RoutingMode:
    return parse_routing_mode(os.environ.get(ROUTING_MODE_FLAG))


def shadow_route_diagnostics(
    question: str,
    *,
    tools: tuple[ToolSpec, ...],
    provider: ToolChoiceProvider,
) -> dict[str, Any]:
    return complete_shadow_route_diagnostics(
        begin_shadow_route_diagnostics(question, tools=tools, provider=provider)
    )


def begin_shadow_route_diagnostics(
    question: str,
    *,
    tools: tuple[ToolSpec, ...],
    provider: ToolChoiceProvider,
) -> ShadowTask[RoutePlan]:
    def plan_route() -> RoutePlan:
        plan = _planner(tools, provider).plan(question, routing_mode=RoutingMode.SHADOW)
        LOGGER.info("v4 shadow plan completed prs=%s", plan.proposal.model_dump_json())
        return plan

    return start_with_budget(plan_route)


def complete_shadow_route_diagnostics(task: ShadowTask[RoutePlan]) -> dict[str, Any]:
    outcome = collect_with_budget(task)
    if outcome.status == "budget_exceeded":
        return {
            "routing_mode": RoutingMode.SHADOW.value,
            "shadow_status": "budget_exceeded",
        }
    if outcome.status == "error":
        LOGGER.warning("v4 shadow planning failed: %s", outcome.error_name)
        return {
            "routing_mode": RoutingMode.SHADOW.value,
            "shadow_status": "error",
            "shadow_error": outcome.error_name,
        }
    assert outcome.value is not None
    return _plan_diagnostics(outcome.value, status_key="shadow_status", status_value="ok")


def internal_legacy_route_diagnostics(
    mode: RoutingMode,
    *,
    runtime_status: str,
) -> dict[str, Any]:
    decision = RoutingDecision(
        source_domain="internal_mart",
        domain_decision_source=DomainDecisionSource.METRIC_OWNER,
        capability_status=CapabilityStatus.SUPPORTED,
        tool_selection_source=ToolSelectionSource.LEGACY_RULE,
        route_outcome=RouteOutcome.CALL,
    )
    proposal = ProposedRoutingSignature(
        routing_mode=mode,
        routing_decision=decision,
    )
    diagnostics: dict[str, Any] = {
        "routing_mode": mode.value,
        "proposed_routing_signature": proposal.model_dump(mode="json"),
        "eligible_tools": [],
        "reason_code": None,
        "repair_count": 0,
        "deterministic_rule_id": "INTERNAL_MART_LEGACY_ROUTE",
    }
    match mode:
        case RoutingMode.SHADOW:
            diagnostics["shadow_status"] = "ok"
        case RoutingMode.ENFORCE:
            signature = ExecutedCallSignature(
                routing_mode=mode,
                routing_decision=decision,
                fallback_reason=None,
                reason_code=None,
                runtime_status=runtime_status,
            )
            diagnostics["executed_call_signature"] = signature.model_dump(mode="json")
            diagnostics["claim_evidence_binding_status"] = "not_applicable"
            diagnostics["claim_evidence_bindings"] = []
        case RoutingMode.OFF:
            raise RoutingV4ContractError("OFF mode does not emit routing v4 diagnostics")
    return diagnostics


def execute_enforced_route(
    question: str,
    *,
    tools: tuple[ToolSpec, ...],
    provider: ToolChoiceProvider,
    completion_policy: CompletionPolicy,
    timing: Timing | None,
) -> EnforcedRouteResult:
    try:
        plan = _planner(tools, provider).plan(question, routing_mode=RoutingMode.ENFORCE)
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK - ENFORCE boundary must fail closed.
        LOGGER.warning("v4 enforce planning failed closed: %s", exc)
        plan = _failed_plan()

    if plan.proposal.routing_decision.route_outcome is not RouteOutcome.CALL:
        result = _typed_result(plan)
    else:
        forced_choices = tuple(
            ToolChoice(
                call.tool_name,
                call.normalized_args,
                "v4 canonical route",
                call_id=f"v4-call-{ordinal}",
            )
            for ordinal, call in enumerate(plan.proposal.proposed_calls, start=1)
        )
        result = AgentExecutor(
            provider=_StopAfterPlanProvider(),
            completion_policy=completion_policy,
            best_effort=True,
            forced_choices=forced_choices,
            parallel_forced_choices=len(forced_choices) > 1,
            timing=timing,
        ).run(user_text=question, tools=tools)
        result, runtime_reason = normalize_execution_result(plan, result)

    if plan.proposal.routing_decision.route_outcome is not RouteOutcome.CALL:
        runtime_reason = plan.reason_code

    binding_status, bindings = claim_evidence_bindings(result)
    if result.status in {"ok", "partial"} and binding_status != "pass":
        result = safe_execution_failure(result, reason_code="EVIDENCE_BINDING_FAILED")
        runtime_reason = "EVIDENCE_BINDING_FAILED"

    signature = _executed_signature(plan, result, runtime_reason=runtime_reason)
    diagnostics = _plan_diagnostics(plan)
    diagnostics["executed_call_signature"] = signature.model_dump(mode="json")
    diagnostics["claim_evidence_binding_status"] = binding_status
    diagnostics["claim_evidence_bindings"] = bindings
    return EnforcedRouteResult(result=result, diagnostics=diagnostics)


def _planner(
    tools: tuple[ToolSpec, ...],
    provider: ToolChoiceProvider,
) -> ExternalRoutePlanner:
    return ExternalRoutePlanner(
        tools=tools,
        provider=provider,
        capability_matrix=default_capability_matrix(),
    )


def _plan_diagnostics(
    plan: RoutePlan,
    *,
    status_key: str | None = None,
    status_value: str | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "routing_mode": plan.proposal.routing_mode.value,
        "proposed_routing_signature": plan.proposal.model_dump(mode="json"),
        "eligible_tools": list(plan.eligible_tools),
        "reason_code": plan.reason_code,
        "repair_count": plan.repair_count,
        "deterministic_rule_id": plan.deterministic_rule_id,
    }
    if status_key is not None:
        diagnostics[status_key] = status_value
    return diagnostics


def _typed_result(plan: RoutePlan) -> AgentResult:
    return AgentResult(
        status="typed_stop",
        answer=plan.typed_message or typed_message(plan.reason_code or "AMBIGUOUS_INPUT"),
        tool_calls=(),
        sources=(),
        traces=(),
        fallback_code=None,
    )


def _executed_signature(
    plan: RoutePlan,
    result: AgentResult,
    *,
    runtime_reason: str | None,
) -> ExecutedCallSignature:
    calls = tuple(
        executed
        for ordinal, proposed in enumerate(plan.proposal.proposed_calls, start=1)
        if (
            executed := _executed_call(
                ordinal,
                proposed.tool_name,
                proposed.normalized_args,
                result,
            )
        )
        is not None
    )
    fallback_reason = result.fallback_code.value if result.fallback_code is not None else None
    return ExecutedCallSignature(
        routing_mode=RoutingMode.ENFORCE,
        routing_decision=plan.proposal.routing_decision,
        proposed_calls=plan.proposal.proposed_calls,
        executed_calls=calls,
        fallback_reason=fallback_reason,
        reason_code=runtime_reason or plan.reason_code or fallback_reason,
        runtime_status=result.status,
    )


def _executed_call(
    ordinal: int,
    tool_name: str,
    normalized_args: dict[str, Any],
    result: AgentResult,
) -> ExecutedCall | None:
    if ordinal <= len(result.tool_calls):
        status = str(result.tool_calls[ordinal - 1].get("status") or "unknown")
    else:
        trace = next(
            (item for item in result.traces if item.step == ordinal and item.tool == tool_name),
            None,
        )
        if trace is None:
            return None
        status = trace.status
    return ExecutedCall(
        call_ordinal=ordinal,
        parent_ordinal=None,
        tool_name=tool_name,
        normalized_args=normalized_args,
        result_status=status,
    )


def _failed_plan() -> RoutePlan:
    decision = RoutingDecision(
        source_domain="unresolved",
        domain_decision_source=DomainDecisionSource.UNRESOLVED,
        capability_status=CapabilityStatus.UNRESOLVED,
        tool_selection_source=ToolSelectionSource.NONE,
        route_outcome=RouteOutcome.TYPED_STOP,
    )
    return RoutePlan(
        proposal=ProposedRoutingSignature(
            routing_mode=RoutingMode.ENFORCE,
            routing_decision=decision,
        ),
        eligible_tools=(),
        reason_code="INVALID_TOOL_ARGUMENTS",
        typed_message=typed_message("INVALID_TOOL_ARGUMENTS"),
    )
