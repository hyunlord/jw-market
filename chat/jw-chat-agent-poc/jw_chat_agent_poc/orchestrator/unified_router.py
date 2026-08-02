from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from jw_chat_agent_poc.contracts.query import ResolvedQuery
from jw_chat_agent_poc.contracts.routing import (
    UNIFIED_ROUTER_SHADOW_ENV,
    CanonicalRouteDecision,
    RejectedRoute,
    RouteFieldComparison,
    RouteMode,
    RouteShadowComparison,
)
from jw_chat_agent_poc.service.context_scope import ContextScope
from jw_chat_agent_poc.service.conversation import ConversationSlots
from jw_chat_agent_poc.service.routing_boundaries import (
    decide_app_scope_route,
    decide_market_shortcut,
)
from jw_chat_agent_poc.service.routing_boundary_contract import (
    MarketRouteKind,
    MarketScopeRoutingPort,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import (
    QuestionClassification,
    classify_question_without_observation,
)
from jw_chat_agent_poc.tool_use.routing_v4_types import DomainDecisionSource


class SecurityVerdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class AppScopeSignals:
    file_question: str
    effective_question: str
    has_file: bool
    is_fresh_upload: bool
    has_market_intent: bool
    has_market_anchor: bool
    file_schema_columns: tuple[str, ...]
    needs_brand_clarification: bool
    needs_market_clarification: bool


@dataclass(frozen=True, slots=True)
class MarketShortcutSignals:
    has_documents: bool
    use_direct_agent_loop: bool
    market_scope_resolver: MarketScopeRoutingPort


@dataclass(frozen=True, slots=True)
class PlannerSignals:
    selected_handler: str
    deterministic_plan: bool
    planner_kind: str


@dataclass(frozen=True, slots=True)
class UnifiedRouteInput:
    question: str
    security_verdict: SecurityVerdict
    resolved_query: ResolvedQuery | None = None
    conversation_slots: ConversationSlots | None = None
    app_scope: AppScopeSignals | None = None
    market_shortcut: MarketShortcutSignals | None = None
    planner: PlannerSignals | None = None
    unresolved_fields: tuple[str, ...] = ()
    deep_research: bool = False


def route(route_input: UnifiedRouteInput) -> CanonicalRouteDecision:
    """Build a canonical hierarchy while leaving legacy execution authoritative."""

    if route_input.security_verdict is SecurityVerdict.BLOCK:
        return CanonicalRouteDecision(
            domain="security",
            handler="security_block",
            execution_mode=RouteMode.DETERMINISTIC,
            decided_layers=("security",),
            reason_codes=("security:blocked",),
        )

    app_decision = (
        decide_app_scope_route(
            file_question=signals.file_question,
            effective_question=signals.effective_question,
            has_file=signals.has_file,
            is_fresh_upload=signals.is_fresh_upload,
            has_market_intent=signals.has_market_intent,
            has_market_anchor=signals.has_market_anchor,
            file_schema_columns=signals.file_schema_columns,
            needs_brand_clarification=signals.needs_brand_clarification,
            needs_market_clarification=signals.needs_market_clarification,
        )
        if (signals := route_input.app_scope) is not None
        else None
    )
    context_scope = app_decision.context_scope.value if app_decision is not None else None

    context_fields = {
        "context_scope": context_scope,
        "context_handler": "context_scope_dispatch" if app_decision is not None else None,
        "context_mode": RouteMode.DETERMINISTIC if app_decision is not None else None,
    }

    if route_input.unresolved_fields:
        return CanonicalRouteDecision(
            **context_fields,
            domain=context_scope or "unresolved",
            handler="clarification",
            execution_mode=RouteMode.DETERMINISTIC,
            decided_layers=("app_scope", "clarification") if app_decision else ("clarification",),
            reason_codes=tuple(f"unresolved:{field}" for field in route_input.unresolved_fields),
            clarification_message="unresolved routing fields require clarification",
        )

    if route_input.deep_research:
        return CanonicalRouteDecision(
            **context_fields,
            domain="research",
            handler="deep_research",
            execution_mode=RouteMode.WORKFLOW,
            tool_plan_owner="deep_research_planner",
            decided_layers=("app_scope", "deep_research") if app_decision else ("deep_research",),
            reason_codes=("intent:deep_research",),
        )

    planner = route_input.planner
    planner_mode = (
        RouteMode.DETERMINISTIC if planner and planner.deterministic_plan else RouteMode.AGENTIC
    )
    planner_fields = {
        "tool_plan_owner": "agent_loop_planner" if planner is not None else None,
        "tool_plan_handler": planner.selected_handler if planner is not None else None,
        "tool_plan_mode": planner_mode if planner is not None else None,
    }

    classification = classify_question_without_observation(route_input.question)
    if classification.source_domain != "unresolved":
        mode = (
            RouteMode.AGENTIC
            if classification.domain_decision_source is DomainDecisionSource.LLM
            else RouteMode.DETERMINISTIC
        )
        layers = ("routing_v4_rules",)
        if app_decision is not None:
            layers = ("app_scope", *layers)
        return CanonicalRouteDecision(
            **context_fields,
            domain=classification.source_domain,
            handler=classification.requested_capability,
            execution_mode=mode,
            capability_domain=classification.source_domain,
            capability=classification.requested_capability,
            capability_mode=mode,
            tool_plan_owner=planner_fields["tool_plan_owner"] or "routing_v4_rules",
            tool_plan_handler=planner_fields["tool_plan_handler"],
            tool_plan_mode=planner_fields["tool_plan_mode"],
            decided_layers=(*layers, *(("agent_loop_planner",) if planner else ())),
            reason_codes=_classification_reason_codes(classification),
            clarification_message=(
                "capability arguments unresolved" if classification.unresolved_arguments else None
            ),
        )

    if (market_signals := route_input.market_shortcut) is not None:
        market_decision = decide_market_shortcut(
            question=route_input.question,
            has_documents=market_signals.has_documents,
            use_direct_agent_loop=market_signals.use_direct_agent_loop,
            market_scope_resolver=market_signals.market_scope_resolver,
        )
        mode = _market_execution_mode(market_decision.kind)
        return CanonicalRouteDecision(
            **{
                **context_fields,
                "context_scope": context_scope or ContextScope.MARKET.value,
            },
            domain="market",
            handler=market_decision.handler,
            execution_mode=mode,
            tool_plan_owner=(
                "agent_loop_planner"
                if planner is not None or mode is RouteMode.AGENTIC
                else "market_shortcut"
            ),
            tool_plan_handler=planner_fields["tool_plan_handler"],
            tool_plan_mode=planner_fields["tool_plan_mode"],
            market_route_kind=market_decision.kind.value,
            decided_layers=("app_scope", "market_shortcut") if app_decision else ("market_shortcut",),
            reason_codes=(market_decision.reason,),
            clarification_message=(
                "market view clarification required"
                if market_decision.kind is MarketRouteKind.MARKET_CLARIFICATION
                else None
            ),
        )

    if app_decision is not None:
        return CanonicalRouteDecision(
            **context_fields,
            domain=context_scope or "unresolved",
            handler="context_scope_dispatch",
            execution_mode=RouteMode.DETERMINISTIC,
            decided_layers=("app_scope",),
            reason_codes=(f"scope:{context_scope}",),
            clarification_message=(
                "scope clarification required"
                if app_decision.needs_scope_clarification
                else None
            ),
        )

    if planner is not None:
        return CanonicalRouteDecision(
            **context_fields,
            domain="agent_loop",
            handler=planner.selected_handler,
            execution_mode=planner_mode,
            **planner_fields,
            decided_layers=("agent_loop_planner",),
            reason_codes=(f"planner_kind:{planner.planner_kind}",),
        )

    return CanonicalRouteDecision(
        **context_fields,
        domain=classification.source_domain,
        handler=classification.requested_capability,
        execution_mode=RouteMode.DETERMINISTIC,
        capability=classification.requested_capability,
        capability_domain=classification.source_domain,
        capability_mode=RouteMode.DETERMINISTIC,
        tool_plan_owner="routing_v4_rules",
        decided_layers=("routing_v4_rules",),
        reason_codes=_classification_reason_codes(classification),
    )


def compare_with_legacy(
    unified: CanonicalRouteDecision,
    *,
    decided_by: str,
    legacy_domain: str,
    legacy_handler: str,
    legacy_mode: RouteMode,
) -> RouteShadowComparison:
    match decided_by:
        case "app_scope":
            fields = (
                _field_comparison("context_scope", legacy_domain, unified.context_scope),
                _field_comparison("context_handler", legacy_handler, unified.context_handler),
                _field_comparison(
                    "context_mode",
                    legacy_mode.value,
                    unified.context_mode.value if unified.context_mode else None,
                ),
            )
        case "routing_v4_rules":
            fields = (
                _field_comparison(
                    "capability_domain", legacy_domain, unified.capability_domain
                ),
                _field_comparison("capability", legacy_handler, unified.capability),
                _field_comparison(
                    "capability_mode",
                    legacy_mode.value,
                    unified.capability_mode.value if unified.capability_mode else None,
                ),
            )
        case "agent_loop_planner":
            fields = (
                _field_comparison("tool_plan_owner", legacy_domain, unified.tool_plan_owner),
                _field_comparison(
                    "tool_plan_handler", legacy_handler, unified.tool_plan_handler
                ),
                _field_comparison(
                    "tool_plan_mode",
                    legacy_mode.value,
                    unified.tool_plan_mode.value if unified.tool_plan_mode else None,
                ),
            )
        case _:
            fields = (
                _field_comparison("domain", legacy_domain, unified.domain),
                _field_comparison("handler", legacy_handler, unified.handler),
                _field_comparison("mode", legacy_mode.value, unified.execution_mode.value),
            )
    mismatches = tuple(item.field for item in fields if item.comparable and not item.matches)
    unavailable = tuple(item.field for item in fields if not item.comparable)
    return RouteShadowComparison(
        decided_by=decided_by,
        matches=not mismatches,
        field_comparisons=fields,
        mismatch_fields=mismatches,
        unavailable_fields=unavailable,
    )


def _field_comparison(field: str, legacy: str | None, unified: str | None) -> RouteFieldComparison:
    return RouteFieldComparison(
        field=field,
        legacy_value=legacy,
        unified_value=unified,
        matches=legacy == unified,
    )


def _classification_reason_codes(
    classification: QuestionClassification,
) -> tuple[str, ...]:
    return tuple(
        code
        for code in (
            classification.deterministic_rule_id,
            f"decision_source:{classification.domain_decision_source.value}",
        )
        if code
    )


def _market_execution_mode(kind: MarketRouteKind) -> RouteMode:
    match kind:
        case (
            MarketRouteKind.REQUESTED_SOURCE_AGENT
            | MarketRouteKind.DIRECT_AGENT_LOOP
            | MarketRouteKind.AGENT_LOOP
        ):
            return RouteMode.AGENTIC
        case (
            MarketRouteKind.EXPLICIT_MARKET_ID
            | MarketRouteKind.MARKET_MEMBERS_BRAND
            | MarketRouteKind.NAMED_MARKET
            | MarketRouteKind.MARKET_CLARIFICATION
            | MarketRouteKind.MARKET_SCOPE_ANSWER
        ):
            return RouteMode.DETERMINISTIC
        case unreachable:
            assert_never(unreachable)


__all__ = (
    "UNIFIED_ROUTER_SHADOW_ENV",
    "AppScopeSignals",
    "MarketShortcutSignals",
    "PlannerSignals",
    "SecurityVerdict",
    "UnifiedRouteInput",
    "compare_with_legacy",
    "route",
)
