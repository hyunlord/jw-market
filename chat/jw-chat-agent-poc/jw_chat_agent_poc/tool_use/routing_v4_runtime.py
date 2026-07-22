from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
import time
from typing import Any

from jw_chat_agent_poc.common.timing import Timing
from jw_chat_agent_poc.orchestrator.source_grading import (
    requested_authority_source_explicit,
)
from jw_chat_agent_poc.tool_use.contracts import AgentResult, EvidenceFact
from jw_chat_agent_poc.tool_use.executor import AgentExecutor, CompletionPolicy
from jw_chat_agent_poc.tool_use.provider import (
    DEFAULT_TOOL_ROUTING_PLANNER_MAX_TOKENS,
    DEFAULT_TOOL_ROUTING_PLANNER_TIMEOUT_S,
    ToolChoice,
    ToolChoiceProvider,
)
from jw_chat_agent_poc.tool_use.routing_v4_capabilities import (
    default_capability_matrix,
)
from jw_chat_agent_poc.tool_use.routing_v4_execution import (
    claim_evidence_bindings,
    normalize_execution_result,
    official_web_fallback_call_cap,
    official_web_fallback_eligible,
    official_web_fallback_policy,
    official_web_fallback_query,
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
    RoutingBudgetTrace,
    RoutingToolCallBudget,
    RoutingV4ContractError,
    RoutingDecision,
    RoutingMode,
    ToolSelectionSource,
    parse_routing_mode,
)
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer


LOGGER = logging.getLogger(__name__)
ROUTING_MODE_FLAG = "CHAT_TOOL_ROUTING_MODE"
_HTTPS_URL_RE = re.compile(r"https://[^\s\])>]+")


@dataclass(frozen=True, slots=True)
class EnforcedRouteResult:
    result: AgentResult
    diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MeasuredRoutePlan:
    plan: RoutePlan
    planner_latency_ms: float


@dataclass(frozen=True, slots=True)
class ShadowRouteTask:
    task: ShadowTask[_MeasuredRoutePlan]
    tools: tuple[ToolSpec, ...]
    provider: ToolChoiceProvider


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
) -> ShadowRouteTask:
    def plan_route() -> _MeasuredRoutePlan:
        started = time.perf_counter()
        plan = _planner(tools, provider).plan(question, routing_mode=RoutingMode.SHADOW)
        planner_latency_ms = _elapsed_ms(started)
        LOGGER.info("v4 shadow plan completed prs=%s", plan.proposal.model_dump_json())
        return _MeasuredRoutePlan(plan=plan, planner_latency_ms=planner_latency_ms)

    return ShadowRouteTask(
        task=start_with_budget(plan_route),
        tools=tools,
        provider=provider,
    )


def complete_shadow_route_diagnostics(task: ShadowRouteTask) -> dict[str, Any]:
    outcome = collect_with_budget(task.task)
    if outcome.status == "budget_exceeded":
        return {
            "routing_mode": RoutingMode.SHADOW.value,
            "shadow_status": "budget_exceeded",
            "budget": _routing_budget_trace(
                plan=None,
                tools=task.tools,
                provider=task.provider,
                planner_latency_ms=None,
                tool_execution_latency_ms=None,
                routing_latency_ms=None,
                executed_calls=None,
            ).model_dump(mode="json"),
        }
    if outcome.status == "error":
        LOGGER.warning("v4 shadow planning failed: %s", outcome.error_name)
        return {
            "routing_mode": RoutingMode.SHADOW.value,
            "shadow_status": "error",
            "shadow_error": outcome.error_name,
            "budget": _routing_budget_trace(
                plan=None,
                tools=task.tools,
                provider=task.provider,
                planner_latency_ms=None,
                tool_execution_latency_ms=None,
                routing_latency_ms=None,
                executed_calls=None,
            ).model_dump(mode="json"),
        }
    assert outcome.value is not None
    measured = outcome.value
    budget = _routing_budget_trace(
        plan=measured.plan,
        tools=task.tools,
        provider=task.provider,
        planner_latency_ms=measured.planner_latency_ms,
        tool_execution_latency_ms=None,
        routing_latency_ms=measured.planner_latency_ms,
        executed_calls=None,
    )
    return _plan_diagnostics(
        measured.plan,
        budget=budget,
        status_key="shadow_status",
        status_value="ok",
    )


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
        "eligible_tools_count": 0,
        "input_key": "market_identifier",
        "reason_code": None,
        "repair_count": 0,
        "deterministic_rule_id": "INTERNAL_MART_LEGACY_ROUTE",
        "budget": _routing_budget_trace(
            plan=None,
            tools=(),
            provider=None,
            planner_latency_ms=0.0,
            tool_execution_latency_ms=None if mode is RoutingMode.SHADOW else 0.0,
            routing_latency_ms=0.0,
            executed_calls=None if mode is RoutingMode.SHADOW else 0,
        ).model_dump(mode="json"),
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
    route_started = time.perf_counter()
    planner_started = time.perf_counter()
    planner_calls_override: int | None = None
    try:
        plan = _planner(tools, provider).plan(question, routing_mode=RoutingMode.ENFORCE)
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK - ENFORCE boundary must fail closed.
        LOGGER.warning("v4 enforce planning failed closed: %s", exc)
        plan = _failed_plan()
        planner_calls_override = 1
    planner_latency_ms = _elapsed_ms(planner_started)

    tool_execution_latency_ms = 0.0
    web_fallback_diagnostics = _web_fallback_diagnostics(
        runtime_reason=plan.reason_code,
    )
    if plan.proposal.routing_decision.route_outcome is not RouteOutcome.CALL:
        result = _typed_result(plan)
    else:
        forced_choices = tuple(
            ToolChoice(
                call.tool_name,
                plan.execution_args[ordinal - 1],
                "v4 canonical route",
                call_id=f"v4-call-{ordinal}",
            )
            for ordinal, call in enumerate(plan.proposal.proposed_calls, start=1)
        )
        tool_execution_started = time.perf_counter()
        result = AgentExecutor(
            provider=_StopAfterPlanProvider(),
            completion_policy=completion_policy,
            best_effort=True,
            forced_choices=forced_choices,
            parallel_forced_choices=len(forced_choices) > 1,
            timing=timing,
        ).run(user_text=question, tools=tools)
        tool_execution_latency_ms = _elapsed_ms(tool_execution_started)
        result, runtime_reason = normalize_execution_result(plan, result)
        result, web_fallback_diagnostics, web_fallback_latency_ms = (
            _apply_official_web_fallback(
                question,
                plan=plan,
                result=result,
                runtime_reason=runtime_reason,
                tools=tools,
                timing=timing,
            )
        )
        tool_execution_latency_ms += web_fallback_latency_ms

    if plan.proposal.routing_decision.route_outcome is not RouteOutcome.CALL:
        runtime_reason = plan.reason_code

    binding_status, bindings = claim_evidence_bindings(result)
    if result.status in {"ok", "partial"} and binding_status != "pass":
        result = safe_execution_failure(result, reason_code="EVIDENCE_BINDING_FAILED")
        runtime_reason = "EVIDENCE_BINDING_FAILED"

    signature = _executed_signature(plan, result, runtime_reason=runtime_reason)
    budget = _routing_budget_trace(
        plan=plan,
        tools=tools,
        provider=provider,
        planner_latency_ms=planner_latency_ms,
        tool_execution_latency_ms=tool_execution_latency_ms,
        routing_latency_ms=_elapsed_ms(route_started),
        executed_calls=len(signature.executed_calls),
        planner_calls_override=planner_calls_override,
    )
    diagnostics = _plan_diagnostics(plan, budget=budget)
    diagnostics["executed_call_signature"] = signature.model_dump(mode="json")
    diagnostics["claim_evidence_binding_status"] = binding_status
    diagnostics["claim_evidence_bindings"] = bindings
    diagnostics["official_web_fallback"] = web_fallback_diagnostics
    return EnforcedRouteResult(result=result, diagnostics=diagnostics)


def _apply_official_web_fallback(
    question: str,
    *,
    plan: RoutePlan,
    result: AgentResult,
    runtime_reason: str | None,
    tools: tuple[ToolSpec, ...],
    timing: Timing | None,
) -> tuple[AgentResult, dict[str, Any], float]:
    source_domain = plan.proposal.routing_decision.source_domain
    reason = runtime_reason or plan.reason_code or ""
    usable_authoritative_results = sum(
        str(call.get("status") or "").lower() in {"ok", "partial"}
        for call in result.tool_calls
    )
    requested_source_explicit = requested_authority_source_explicit(
        question,
        source_domain=source_domain,
    )
    eligible = official_web_fallback_eligible(
        source_domain=source_domain,
        runtime_reason=reason,
        usable_authoritative_results=usable_authoritative_results,
        requested_source_explicit=requested_source_explicit,
    )
    diagnostics = _web_fallback_diagnostics(
        runtime_reason=(
            "PARTIAL_RESULT"
            if usable_authoritative_results
            else "EXPLICIT_SOURCE_NO_FALLBACK"
            if requested_source_explicit
            else reason
        ),
        eligible=eligible,
        requested_source_explicit=requested_source_explicit,
    )
    if not eligible:
        return result, diagnostics, 0.0

    web_tool = next((tool for tool in tools if tool.name == "web_search"), None)
    if web_tool is None:
        diagnostics["reason_code"] = "WEB_TOOL_UNAVAILABLE"
        return result, diagnostics, 0.0

    started = time.perf_counter()
    web_result = AgentExecutor(
        provider=_StopAfterPlanProvider(),
        completion_policy=None,
        best_effort=False,
        forced_choices=(
            ToolChoice(
                "web_search",
                {
                    "query": official_web_fallback_query(
                        question,
                        source_domain=source_domain,
                    ),
                    "brand": None,
                    "topic": "general",
                },
                "official web fallback after authoritative upstream failure",
                call_id="v4-official-web-fallback",
            ),
        ),
        timing=timing,
    ).run(user_text=question, tools=(web_tool,))
    latency_ms = _elapsed_ms(started)
    diagnostics["calls_executed"] = 1

    decision = official_web_fallback_policy(
        source_domain=source_domain,
        runtime_reason=reason,
        usable_authoritative_results=usable_authoritative_results,
        candidate_urls=_web_result_urls(web_result),
        requested_source_explicit=requested_source_explicit,
    )
    diagnostics.update(
        {
            "accepted_urls": list(decision.accepted_urls),
            "separate_section": decision.separate_section,
            "reason_code": decision.reason_code,
        }
    )
    if not decision.accepted_urls:
        return result, diagnostics, latency_ms

    return (
        _combine_official_web_result(
            result,
            web_result,
            accepted_urls=decision.accepted_urls,
            disclosure=decision.disclosure,
        ),
        diagnostics,
        latency_ms,
    )


def _web_fallback_diagnostics(
    *,
    runtime_reason: str | None,
    eligible: bool = False,
    requested_source_explicit: bool = False,
) -> dict[str, Any]:
    return {
        "enabled": official_web_fallback_call_cap() == 1,
        "eligible": eligible,
        "requested_source_explicit": requested_source_explicit,
        "calls_executed": 0,
        "accepted_urls": [],
        "separate_section": False,
        "reason_code": runtime_reason,
    }


def _web_result_urls(result: AgentResult) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            url
            for call in result.tool_calls
            for fact in _call_evidence(call)
            for url in _HTTPS_URL_RE.findall(str(fact.get("source_locator") or ""))
        )
    )


def _combine_official_web_result(
    authority_result: AgentResult,
    web_result: AgentResult,
    *,
    accepted_urls: tuple[str, ...],
    disclosure: str,
) -> AgentResult:
    accepted = set(accepted_urls)
    facts: list[EvidenceFact] = []
    filtered_calls: list[dict[str, Any]] = []
    for call in web_result.tool_calls:
        filtered_evidence: list[dict[str, Any]] = []
        for raw_fact in _call_evidence(call):
            locator = str(raw_fact.get("source_locator") or "")
            if not any(url in accepted for url in _HTTPS_URL_RE.findall(locator)):
                continue
            fact = EvidenceFact.model_validate(raw_fact).model_copy(
                update={"source_name": "[SUPPLEMENTARY] 웹 검색 결과"}
            )
            facts.append(fact)
            filtered_evidence.append(fact.model_dump(mode="json"))
        if not filtered_evidence:
            continue
        filtered_call = dict(call)
        render_data = dict(filtered_call.get("render_data") or {})
        render_data["evidence"] = filtered_evidence
        filtered_call["render_data"] = render_data
        filtered_calls.append(filtered_call)

    appendix = render_evidence_answer(tuple(facts))
    answer = "\n\n".join(
        part
        for part in (
            disclosure,
            f"### 공식 도메인 보조 자료\n{appendix}",
        )
        if part
    )
    trace_offset = len(authority_result.traces)
    web_traces = tuple(
        trace.model_copy(update={"step": trace_offset + ordinal})
        for ordinal, trace in enumerate(web_result.traces, start=1)
    )
    return AgentResult(
        status="partial",
        answer=answer,
        tool_calls=authority_result.tool_calls + tuple(filtered_calls),
        sources=("[SUPPLEMENTARY] 웹 검색 결과",),
        traces=authority_result.traces + web_traces,
        fallback_code=None,
    )


def _call_evidence(call: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    render_data = call.get("render_data")
    if not isinstance(render_data, dict):
        return ()
    evidence = render_data.get("evidence")
    if not isinstance(evidence, list):
        return ()
    return tuple(fact for fact in evidence if isinstance(fact, dict))


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
    budget: RoutingBudgetTrace,
    status_key: str | None = None,
    status_value: str | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "routing_mode": plan.proposal.routing_mode.value,
        "proposed_routing_signature": plan.proposal.model_dump(mode="json"),
        "eligible_tools": list(plan.eligible_tools),
        "eligible_tools_count": len(plan.eligible_tools),
        "input_key": plan.input_key,
        "reason_code": plan.reason_code,
        "repair_count": plan.repair_count,
        "deterministic_rule_id": plan.deterministic_rule_id,
        "budget": budget.model_dump(mode="json"),
    }
    if status_key is not None:
        diagnostics[status_key] = status_value
    return diagnostics


def _routing_budget_trace(
    *,
    plan: RoutePlan | None,
    tools: tuple[ToolSpec, ...],
    provider: ToolChoiceProvider | None,
    planner_latency_ms: float | None,
    tool_execution_latency_ms: float | None,
    routing_latency_ms: float | None,
    executed_calls: int | None,
    planner_calls_override: int | None = None,
) -> RoutingBudgetTrace:
    proposed_calls = plan.proposal.proposed_calls if plan is not None else ()
    planned_count = len(proposed_calls)
    if planned_count > 5:
        raise RoutingV4ContractError("authority tool call plan exceeds the v4 cap")
    authority_cap = 5 if planned_count > 1 else planned_count
    by_name = {tool.name: tool for tool in tools}
    timeout_budgets: list[RoutingToolCallBudget] = []
    for ordinal, call in enumerate(proposed_calls, start=1):
        spec = by_name.get(call.tool_name)
        if spec is None:
            raise RoutingV4ContractError("proposed tool is missing from the runtime registry")
        timeout_budgets.append(
            RoutingToolCallBudget(
                call_ordinal=ordinal,
                tool_name=call.tool_name,
                timeout_s=spec.timeout_s,
            )
        )
    planner_calls_used = planner_calls_override
    if planner_calls_used is None and plan is not None:
        selection_source = plan.proposal.routing_decision.tool_selection_source
        planner_calls_used = (
            1 + plan.repair_count if selection_source is ToolSelectionSource.LLM else 0
        )
    planner_token_cap = getattr(provider, "max_tokens", None)
    return RoutingBudgetTrace(
        planner_calls_used=planner_calls_used,
        planner_timeout_s=float(
            getattr(provider, "timeout_s", DEFAULT_TOOL_ROUTING_PLANNER_TIMEOUT_S)
        ),
        planner_token_cap=int(planner_token_cap or DEFAULT_TOOL_ROUTING_PLANNER_MAX_TOKENS),
        authority_tool_call_cap=authority_cap,
        authority_tool_calls_planned=planned_count,
        authority_tool_calls_executed=executed_calls,
        official_web_fallback_call_cap=official_web_fallback_call_cap(),
        tool_call_timeouts=tuple(timeout_budgets),
        planner_latency_ms=planner_latency_ms,
        tool_execution_latency_ms=tool_execution_latency_ms,
        routing_latency_ms=routing_latency_ms,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


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
        input_key="unknown",
        reason_code="INVALID_TOOL_ARGUMENTS",
        typed_message=typed_message("INVALID_TOOL_ARGUMENTS"),
    )
