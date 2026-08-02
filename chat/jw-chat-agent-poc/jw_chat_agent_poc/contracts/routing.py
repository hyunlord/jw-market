from __future__ import annotations

from enum import StrEnum
import os

from pydantic import Field

from .base import ContractModel


class RouteMode(StrEnum):
    DETERMINISTIC = "deterministic"
    WORKFLOW = "workflow"
    AGENTIC = "agentic"


UNIFIED_ROUTER_SHADOW_ENV = "JW_CHAT_UNIFIED_ROUTER_SHADOW"


def unified_router_shadow_enabled() -> bool:
    return os.getenv(UNIFIED_ROUTER_SHADOW_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class RejectedRoute(ContractModel):
    domain: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)


class RouteDecision(ContractModel):
    domain: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    mode: RouteMode
    decided_by: str = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    rejected_alternatives: tuple[RejectedRoute, ...] = ()
    clarification_message: str | None = None


class CanonicalRouteDecision(ContractModel):
    """One hierarchical route without collapsing context into execution mode."""

    context_scope: str | None = None
    context_handler: str | None = None
    context_mode: RouteMode | None = None
    domain: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    execution_mode: RouteMode
    capability_domain: str | None = None
    capability: str | None = None
    capability_mode: RouteMode | None = None
    tool_plan_owner: str | None = None
    tool_plan_handler: str | None = None
    tool_plan_mode: RouteMode | None = None
    market_route_kind: str | None = None
    decided_layers: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    rejected_alternatives: tuple[RejectedRoute, ...] = ()
    clarification_message: str | None = None


class RouteFieldComparison(ContractModel):
    field: str = Field(min_length=1)
    legacy_value: str | None = None
    unified_value: str | None = None
    comparable: bool = True
    matches: bool = False
    reason: str | None = None


class RouteShadowComparison(ContractModel):
    decided_by: str = Field(min_length=1)
    matches: bool
    field_comparisons: tuple[RouteFieldComparison, ...] = ()
    mismatch_fields: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()
