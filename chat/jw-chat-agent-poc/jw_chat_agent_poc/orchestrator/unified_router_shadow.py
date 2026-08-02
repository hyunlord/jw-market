from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

from jw_chat_agent_poc.contracts.routing import RouteMode, unified_router_shadow_enabled
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    current_shadow_request_id,
    question_fingerprint,
)
from jw_chat_agent_poc.orchestrator.unified_router import (
    AppScopeSignals,
    MarketShortcutSignals,
    PlannerSignals,
    SecurityVerdict,
    UnifiedRouteInput,
    compare_with_legacy,
    route,
)
from jw_chat_agent_poc.service.routing_boundary_contract import MarketScopeRoutingPort


_LOGGER = logging.getLogger(__name__)


def observe_app_scope_route(
    *,
    question: str,
    file_question: str,
    effective_question: str,
    has_file: bool,
    is_fresh_upload: bool,
    has_market_intent: bool,
    has_market_anchor: bool,
    file_schema_columns: tuple[str, ...],
    needs_brand_clarification: bool,
    needs_market_clarification: bool,
    legacy_domain: str,
    legacy_handler: str,
    legacy_mode: RouteMode,
    deep_research: bool,
) -> None:
    observe_unified_route(
        route_input=UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
            app_scope=AppScopeSignals(
                file_question=file_question,
                effective_question=effective_question,
                has_file=has_file,
                is_fresh_upload=is_fresh_upload,
                has_market_intent=has_market_intent,
                has_market_anchor=has_market_anchor,
                file_schema_columns=file_schema_columns,
                needs_brand_clarification=needs_brand_clarification,
                needs_market_clarification=needs_market_clarification,
            ),
            deep_research=deep_research,
        ),
        decided_by="app_scope",
        legacy_domain=legacy_domain,
        legacy_handler=legacy_handler,
        legacy_mode=legacy_mode,
    )


def observe_market_shortcut_route(
    *,
    question: str,
    has_documents: bool,
    use_direct_agent_loop: bool,
    market_scope_resolver: MarketScopeRoutingPort,
    legacy_domain: str,
    legacy_handler: str,
    legacy_mode: RouteMode,
) -> None:
    observe_unified_route(
        route_input=UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
            market_shortcut=MarketShortcutSignals(
                has_documents=has_documents,
                use_direct_agent_loop=use_direct_agent_loop,
                market_scope_resolver=market_scope_resolver,
            ),
        ),
        decided_by="market_shortcut",
        legacy_domain=legacy_domain,
        legacy_handler=legacy_handler,
        legacy_mode=legacy_mode,
    )


def observe_routing_v4_route(
    *,
    question: str,
    legacy_domain: str,
    legacy_handler: str,
    legacy_mode: RouteMode,
) -> None:
    observe_unified_route(
        route_input=UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
        ),
        decided_by="routing_v4_rules",
        legacy_domain=legacy_domain,
        legacy_handler=legacy_handler,
        legacy_mode=legacy_mode,
    )


def observe_agent_planner_route(
    *,
    question: str,
    selected_handler: str,
    deterministic_plan: bool,
    planner_kind: str,
    legacy_mode: RouteMode,
) -> None:
    observe_unified_route(
        route_input=UnifiedRouteInput(
            question=question,
            security_verdict=SecurityVerdict.ALLOW,
            planner=PlannerSignals(
                selected_handler=selected_handler,
                deterministic_plan=deterministic_plan,
                planner_kind=planner_kind,
            ),
        ),
        decided_by="agent_loop_planner",
        legacy_domain="agent_loop_planner",
        legacy_handler=selected_handler,
        legacy_mode=legacy_mode,
    )


def observe_unified_route(
    *,
    route_input: UnifiedRouteInput,
    decided_by: str,
    legacy_domain: str,
    legacy_handler: str,
    legacy_mode: RouteMode,
) -> None:
    """Compare the unified projection without participating in execution."""

    if not unified_router_shadow_enabled():
        return
    try:
        decision = route(route_input)
        comparison = compare_with_legacy(
            decision,
            decided_by=decided_by,
            legacy_domain=legacy_domain,
            legacy_handler=legacy_handler,
            legacy_mode=legacy_mode,
        )
        from jw_chat_agent_poc.service.runtime_provenance import release_identity_payload

        identity = release_identity_payload()
        _write_payload(
            {
                "event": "unified_router_shadow_comparison",
                "observation_schema_version": 1,
                "request_id": current_shadow_request_id(),
                "observation_id": uuid4().hex,
                "event_timestamp_utc": datetime.now(UTC).isoformat(),
                "pod_name": os.environ.get("HOSTNAME") or "unknown",
                "git_sha": identity["git_sha"],
                "image_digest": identity["image_digest"],
                "mode": "SHADOW",
                "answer_action": "unchanged",
                "question_fingerprint": question_fingerprint(route_input.question),
                "legacy_decided_by": decided_by,
                "canonical_route": decision.model_dump(mode="json"),
                "comparison": comparison.model_dump(mode="json"),
            }
        )
    except Exception:  # noqa: BLE001 - shadow comparison cannot alter execution
        _LOGGER.exception("unified_router_shadow_comparison_failed")


def _write_payload(payload: dict[str, object]) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sys.stdout.write(f"{serialized}\n")
    sys.stdout.flush()
