from __future__ import annotations

from jw_chat_agent_poc.contracts.routing import CanonicalRouteDecision, RouteMode
from jw_chat_agent_poc.orchestrator.unified_router import (
    MarketShortcutSignals,
    SecurityVerdict,
    UnifiedRouteInput,
    route,
)
from jw_chat_agent_poc.service.routing_boundary_contract import MarketScopeRoutingPort


HIRA_REIMBURSEMENT_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_HIRA_REIMBURSEMENT"
_HIRA_REIMBURSEMENT_CAPABILITY = "HIRA_REIMBURSEMENT_CRITERIA"


def select_hira_reimbursement_cutover(
    *,
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    market_scope_resolver: MarketScopeRoutingPort,
) -> CanonicalRouteDecision | None:
    """Return the canonical route only for the first, single-facet cutover."""

    if has_documents:
        return None
    decision = route(
        UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
            market_shortcut=MarketShortcutSignals(
                has_documents=False,
                use_direct_agent_loop=use_direct_agent_loop,
                market_scope_resolver=market_scope_resolver,
            ),
        )
    )
    requested = decision.requested_capabilities
    if requested and requested != (_HIRA_REIMBURSEMENT_CAPABILITY,):
        return None
    if (
        decision.domain != "hira"
        or decision.handler != _HIRA_REIMBURSEMENT_CAPABILITY
        or decision.capability != _HIRA_REIMBURSEMENT_CAPABILITY
        or decision.execution_mode is not RouteMode.DETERMINISTIC
    ):
        return None
    return decision


__all__ = (
    "HIRA_REIMBURSEMENT_CUTOVER_ENV",
    "select_hira_reimbursement_cutover",
)
