from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


MarketView = Literal["market_landscape", "competitive_dynamics", "general_view"]


@dataclass(frozen=True, slots=True)
class MarketScopeIntent:
    brand_hint: str
    metric: str
    view_type: MarketView | None
    requires_clarification: bool


def detect_market_scope_intent(question: str) -> MarketScopeIntent | None:
    normalized = _normalize(question)
    if not any(token in normalized for token in ("같은시장", "시장전체", "해당시장")):
        return None
    view_type = _explicit_view(normalized)
    return MarketScopeIntent(
        brand_hint=_brand_hint(question),
        metric="sales",
        view_type=view_type or "market_landscape",
        requires_clarification=False,
    )


def map_market_view_reply(text: str) -> MarketView | None:
    normalized = _normalize(text)
    if _explicit_view(normalized) == "market_landscape":
        return "market_landscape"
    if _explicit_view(normalized) == "competitive_dynamics":
        return "competitive_dynamics"
    if _explicit_view(normalized) == "general_view":
        return "general_view"
    return None


def _explicit_view(normalized: str) -> MarketView | None:
    if any(token in normalized for token in ("일반뷰", "일반view", "atc4", "atc기준")):
        return "general_view"
    if any(token in normalized for token in ("경쟁군", "경쟁시장", "competitive_dynamics", "competitive", "narrower", "cd기준")):
        return "competitive_dynamics"
    if any(token in normalized for token in ("전략뷰", "전략view", "market_landscape", "ml기준")):
        return "market_landscape"
    return None


def _brand_hint(question: str) -> str:
    match = re.search(r"([가-힣A-Za-z0-9+]+?)(?:이랑|랑|와|과)?\s*같은\s*시장", question)
    if match:
        return match.group(1)
    return ""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()
