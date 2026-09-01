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
HIRA_DISEASE_STATS_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_HIRA_DISEASE_STATS"
MFDS_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_MFDS"
CLINICAL_TRIALS_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_CLINICAL_TRIALS"
CLINICAL_FB02_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_CLINICAL_FB02"
_HIRA_REIMBURSEMENT_CAPABILITY = "HIRA_REIMBURSEMENT_CRITERIA"
_HIRA_DISEASE_STATS_CAPABILITY = "HIRA_DISEASE_PATIENT_STATS"
_MFDS_CAPABILITIES = frozenset(
    {"MFDS_BASIC_PRODUCT_INFO", "MFDS_PERMISSION_DETAIL_FIELDS"}
)
_CLINICAL_TRIAL_CAPABILITIES = frozenset(
    {"CLINICAL_TRIAL_NCT_DETAIL_FIELDS", "CLINICAL_TRIAL_SEARCH"}
)


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


def select_hira_disease_stats_cutover(
    *,
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    market_scope_resolver: MarketScopeRoutingPort,
) -> CanonicalRouteDecision | None:
    """Select only single-facet deterministic HIRA disease-stat routes."""

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
    if requested and requested != (_HIRA_DISEASE_STATS_CAPABILITY,):
        return None
    if (
        decision.domain != "hira"
        or decision.handler != _HIRA_DISEASE_STATS_CAPABILITY
        or decision.capability != _HIRA_DISEASE_STATS_CAPABILITY
        or decision.execution_mode is not RouteMode.DETERMINISTIC
    ):
        return None
    return decision


def select_mfds_cutover(
    *,
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    market_scope_resolver: MarketScopeRoutingPort,
) -> CanonicalRouteDecision | None:
    """Select only single-facet deterministic MFDS routes."""

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
    capability = decision.capability
    requested = decision.requested_capabilities
    if capability not in _MFDS_CAPABILITIES:
        return None
    if requested and requested != (capability,):
        return None
    if (
        decision.domain != "regulatory"
        or decision.handler != capability
        or decision.execution_mode is not RouteMode.DETERMINISTIC
    ):
        return None
    return decision


def select_clinical_trials_cutover(
    *,
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    market_scope_resolver: MarketScopeRoutingPort,
) -> CanonicalRouteDecision | None:
    """Select only canonical ClinicalTrials detail and search routes."""

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
    capability = decision.capability
    requested = decision.requested_capabilities
    if capability not in _CLINICAL_TRIAL_CAPABILITIES:
        return None
    if requested and requested != (capability,):
        return None
    if (
        decision.domain != "clinical_trials"
        or decision.handler != capability
        or decision.execution_mode not in {RouteMode.DETERMINISTIC, RouteMode.AGENTIC}
    ):
        return None
    return decision


__all__ = (
    "CLINICAL_FB02_CUTOVER_ENV",
    "CLINICAL_TRIALS_CUTOVER_ENV",
    "HIRA_DISEASE_STATS_CUTOVER_ENV",
    "HIRA_REIMBURSEMENT_CUTOVER_ENV",
    "MFDS_CUTOVER_ENV",
    "select_hira_disease_stats_cutover",
    "select_hira_reimbursement_cutover",
    "select_mfds_cutover",
    "select_clinical_trials_cutover",
)
