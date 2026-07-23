from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


MarketView = Literal["market_landscape", "competitive_dynamics", "general_view"]


_MARKET_SCOPE_RE = re.compile(
    r"(?:"
    r"같은\s*시장|"
    r"동일(?:한)?\s*시장|"
    r"해당\s*시장|"
    r"(?:속한|소속(?:된)?|포함(?:된)?)\s*시장|"
    r"시장\s*(?:의\s*)?(?:전체\s*)?(?:규모|총\s*매출|전체\s*매출|HHI|CR\s*5|집중도)"
    r")",
    re.IGNORECASE,
)
_BRAND_TOKEN_RE = re.compile(r"([가-힣A-Za-z0-9+_-]{2,80})\s*$")
_BRAND_PARTICLES = ("이랑", "랑", "와", "과", "은", "는", "이", "가", "의")


@dataclass(frozen=True, slots=True)
class MarketScopeIntent:
    brand_hint: str
    metric: str
    view_type: MarketView | None
    requires_clarification: bool


def detect_market_scope_intent(question: str) -> MarketScopeIntent | None:
    scope_match = _MARKET_SCOPE_RE.search(question)
    if scope_match is None:
        return None
    normalized = _normalize(question)
    view_type = _explicit_view(normalized)
    return MarketScopeIntent(
        brand_hint=_brand_hint(question, scope_match),
        metric=_metric(normalized),
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


def asks_market_members(question: str) -> bool:
    normalized = _normalize(question)
    member_noun = any(token in normalized for token in ("브랜드", "제품", "품목", "구성원"))
    list_cue = any(
        token in normalized
        for token in ("목록", "어떤", "뭐", "무엇", "포함", "들어", "나열", "전부", "전체")
    )
    market_context = any(token in normalized for token in ("시장", "기타", "순위", "상위"))
    terse_market_members = "시장" in normalized and normalized.endswith(("브랜드", "제품", "품목", "구성원"))
    return (member_noun and market_context and (list_cue or terse_market_members)) or asks_other_market_members(
        question
    )


def asks_other_market_members(question: str) -> bool:
    normalized = _normalize(question)
    if "기타" in normalized:
        return any(token in normalized for token in ("브랜드", "제품", "품목", "구성원", "시장", "순위", "목록"))
    return "나머지" in normalized and any(token in normalized for token in ("상위", "시장", "브랜드", "제품"))


def _explicit_view(normalized: str) -> MarketView | None:
    if any(token in normalized for token in ("일반뷰", "일반view", "atc4", "atc기준")):
        return "general_view"
    if any(token in normalized for token in ("경쟁군", "경쟁시장", "competitive_dynamics", "competitive", "narrower", "cd기준")):
        return "competitive_dynamics"
    if any(token in normalized for token in ("전략뷰", "전략view", "market_landscape", "ml기준")):
        return "market_landscape"
    return None


def _metric(normalized: str) -> str:
    if "hhi" in normalized:
        return "hhi"
    if "cr5" in normalized:
        return "cr5"
    if "집중도" in normalized:
        return "concentration"
    return "sales"


def _brand_hint(question: str, scope_match: re.Match[str]) -> str:
    prefix = question[: scope_match.start()].rstrip()
    match = _BRAND_TOKEN_RE.search(prefix)
    if match is None:
        return ""
    token = match.group(1)
    for particle in _BRAND_PARTICLES:
        if token.endswith(particle) and len(token) > len(particle) + 1:
            return token[: -len(particle)]
    return token


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()
