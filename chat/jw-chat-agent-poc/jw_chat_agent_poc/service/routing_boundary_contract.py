from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from jw_chat_agent_poc.service.context_scope import ContextScope

if TYPE_CHECKING:
    from jw_chat_agent_poc.tools.metrics.market_scope_intent import MarketScopeIntent


ROUTING_BOUNDARIES_ENV = "JW_CHAT_ROUTING_BOUNDARIES_ENABLED"


class MarketScopeRoutingPort(Protocol):
    def has_explicit_brand_anchor(self, question: str) -> bool: ...

    def has_explicit_named_market(self, question: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class AppScopeDecision:
    context_scope: ContextScope
    needs_scope_clarification: bool


class MarketRouteKind(StrEnum):
    EXPLICIT_MARKET_ID = "explicit_market_id"
    REQUESTED_SOURCE_AGENT = "requested_source_agent"
    MARKET_MEMBERS_BRAND = "market_members_brand"
    NAMED_MARKET = "named_market"
    DIRECT_AGENT_LOOP = "direct_agent_loop"
    AGENT_LOOP = "agent_loop"
    MARKET_CLARIFICATION = "market_clarification"
    MARKET_SCOPE_ANSWER = "market_scope_answer"


@dataclass(frozen=True, slots=True)
class MarketShortcutDecision:
    kind: MarketRouteKind
    handler: str
    reason: str
    market_id: str | None = None
    period: str | None = None
    intent: MarketScopeIntent | None = None


def routing_boundaries_enabled() -> bool:
    return os.getenv(ROUTING_BOUNDARIES_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
