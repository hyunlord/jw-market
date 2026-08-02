from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from jw_chat_agent_poc.agent_loop import should_use_agent_loop
from jw_chat_agent_poc.agent_loop.element_ledger import market_scope_defers_to_contract
from jw_chat_agent_poc.common.periods import requested_period
from jw_chat_agent_poc.orchestrator.source_trap import requested_unavailable_source
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_use_requirements
from jw_chat_agent_poc.service.context_scope import (
    ContextScope,
    has_file_reference,
    matches_file_schema,
    resolve_context_scope,
)
from jw_chat_agent_poc.service.routing_boundary_contract import (
    AppScopeDecision,
    MarketRouteKind,
    MarketScopeRoutingPort,
    MarketShortcutDecision,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tool_use.routing_v4_runtime import configured_routing_mode
from jw_chat_agent_poc.tool_use.routing_v4_types import RoutingMode
from jw_chat_agent_poc.tools.metrics.market_scope import asks_market_members, detect_market_scope_intent
from jw_chat_agent_poc.tools.metrics.market_scope_intent import MarketScopeIntent


_EXPLICIT_MARKET_RE = re.compile(
    r"(?<![A-Za-z0-9_])(ml_\d+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def decide_app_scope_route(
    *,
    file_question: str,
    effective_question: str,
    has_file: bool,
    is_fresh_upload: bool,
    has_market_intent: bool,
    has_market_anchor: bool,
    file_schema_columns: Sequence[str],
    needs_brand_clarification: bool,
    needs_market_clarification: bool,
    resolve_context_scope_fn: Callable[..., ContextScope] = resolve_context_scope,
    matches_file_schema_fn: Callable[..., bool] = matches_file_schema,
    has_file_reference_fn: Callable[[str], bool] = has_file_reference,
) -> AppScopeDecision:
    context_scope = resolve_context_scope_fn(
        file_question,
        has_active_file=has_file,
        is_fresh_upload=is_fresh_upload,
        has_market_intent=has_market_intent,
        has_market_anchor=has_market_anchor,
        file_schema_columns=file_schema_columns,
    )
    if needs_brand_clarification or needs_market_clarification:
        context_scope = ContextScope.MARKET
    file_schema_match = matches_file_schema_fn(file_question, file_schema_columns)
    needs_scope_clarification = (
        has_file
        and has_market_intent
        and not has_market_anchor
        and not has_file_reference_fn(effective_question)
        and not file_schema_match
    )
    return AppScopeDecision(
        context_scope=context_scope,
        needs_scope_clarification=needs_scope_clarification,
    )


def decide_market_shortcut(
    *,
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    market_scope_resolver: MarketScopeRoutingPort,
    should_use_agent_loop_fn: Callable[..., bool] = should_use_agent_loop,
    requested_unavailable_source_fn: Callable[[str], object | None] = requested_unavailable_source,
    asks_market_members_fn: Callable[[str], bool] = asks_market_members,
    detect_market_scope_intent_fn: Callable[[str], MarketScopeIntent | None] = detect_market_scope_intent,
    market_scope_defers_to_contract_fn: Callable[[str], bool] = market_scope_defers_to_contract,
    tool_use_requirements_fn: Callable[[str], tuple[object, ...]] = tool_use_requirements,
    v4_enforces_external_question_fn: Callable[[str], bool] | None = None,
    requested_period_fn: Callable[[str], str | None] = requested_period,
) -> MarketShortcutDecision:
    explicit_market = _EXPLICIT_MARKET_RE.search(question)
    if explicit_market is not None and "시장" in question:
        return MarketShortcutDecision(
            kind=MarketRouteKind.EXPLICIT_MARKET_ID,
            handler="answer_market_id",
            reason="explicit_market_id",
            market_id=explicit_market.group(1).lower(),
            period=requested_period_fn(question) or "latest",
        )
    if requested_unavailable_source_fn(question) is not None and not has_documents:
        return MarketShortcutDecision(
            kind=MarketRouteKind.REQUESTED_SOURCE_AGENT,
            handler="agent_loop",
            reason="requested_source_unavailable",
        )
    intent = detect_market_scope_intent_fn(question)
    if asks_market_members_fn(question) and not has_documents:
        if market_scope_resolver.has_explicit_brand_anchor(question):
            return MarketShortcutDecision(
                kind=MarketRouteKind.MARKET_MEMBERS_BRAND,
                handler="answer_market_landscape",
                reason="market_members_brand",
                intent=intent,
            )
        if market_scope_resolver.has_explicit_named_market(question):
            return MarketShortcutDecision(
                kind=MarketRouteKind.NAMED_MARKET,
                handler="answer_named_market",
                reason="named_market",
                intent=intent,
            )
    agent_loop_required = should_use_agent_loop_fn(question)
    has_brand_anchor = False
    if not agent_loop_required and should_use_agent_loop_fn(question, has_brand_anchor=True):
        has_brand_anchor = market_scope_resolver.has_explicit_brand_anchor(question)
        agent_loop_required = has_brand_anchor
    v4_enforced = v4_enforces_external_question_fn or _v4_enforces_external_question
    if (
        use_direct_agent_loop
        and agent_loop_required
        and not has_documents
        and not tool_use_requirements_fn(question)
        and not v4_enforced(question)
    ):
        return MarketShortcutDecision(
            kind=MarketRouteKind.DIRECT_AGENT_LOOP,
            handler="direct_agent_loop",
            reason="direct_agent_loop_required",
            intent=intent,
        )
    if agent_loop_required:
        return MarketShortcutDecision(
            kind=MarketRouteKind.AGENT_LOOP,
            handler="agent_loop",
            reason="agent_loop_required",
            intent=intent,
        )
    if intent is not None and not has_brand_anchor:
        has_brand_anchor = market_scope_resolver.has_explicit_brand_anchor(question)
    if intent is not None and market_scope_defers_to_contract_fn(question):
        intent = None
    if intent is not None and has_brand_anchor:
        if intent.requires_clarification:
            return MarketShortcutDecision(
                kind=MarketRouteKind.MARKET_CLARIFICATION,
                handler="market_clarification",
                reason="view_clarification_required",
                intent=intent,
            )
        if intent.view_type is not None:
            return MarketShortcutDecision(
                kind=MarketRouteKind.MARKET_SCOPE_ANSWER,
                handler="market_scope_answer",
                reason=f"view:{intent.view_type}",
                intent=intent,
            )
    return MarketShortcutDecision(
        kind=MarketRouteKind.AGENT_LOOP,
        handler="agent_loop",
        reason="market_shortcut_not_selected",
        intent=intent,
    )


def _v4_enforces_external_question(question: str) -> bool:
    return (
        configured_routing_mode() is RoutingMode.ENFORCE
        and classify_question(question).source_domain
        in {"hira", "regulatory", "clinical_trials"}
    )
