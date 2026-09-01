from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final


PORTFOLIO_SCOPE: Final = "portfolio"
SINGLE_BRAND_SCOPE: Final = "single_brand"

_PORTFOLIO_SCOPE_RE: Final = re.compile(
    r"jw\s*(주요|전략|자사|전체)?\s*브랜드|"
    r"(주요|전략|자사|전체)\s*브랜드|"
    r"jw\s*포트폴리오|포트폴리오|"
    r"자사\s*(제품|브랜드)|우리\s*(제품|브랜드)",
    flags=re.IGNORECASE,
)
_DECLINE_RE: Final = re.compile(
    r"하락|감소|떨어|내려|잃|뺏|가져갔|위축|부진|밀리|밀리는|약세|빠진|빠지는"
)
_MARKET_SHARE_RE: Final = re.compile(r"시장\s*점유율|점유율|\bMS\b|market\s*share", flags=re.IGNORECASE)


def portfolio_scope_for_question(question: str) -> str:
    """Classify whether the user asks about the company portfolio, not one brand."""

    normalized = question.strip()
    if not normalized:
        return SINGLE_BRAND_SCOPE
    if not _PORTFOLIO_SCOPE_RE.search(normalized):
        return SINGLE_BRAND_SCOPE
    if not _DECLINE_RE.search(normalized):
        return SINGLE_BRAND_SCOPE
    return PORTFOLIO_SCOPE


def is_portfolio_decline_question(question: str, routes: Sequence[object] = ()) -> bool:
    """Detect company/portfolio-scope decline questions from router scope or text."""

    if any(getattr(route, "scope", SINGLE_BRAND_SCOPE) == PORTFOLIO_SCOPE for route in routes):
        return bool(_DECLINE_RE.search(question))
    if portfolio_scope_for_question(question) == PORTFOLIO_SCOPE:
        return True
    if not _MARKET_SHARE_RE.search(question):
        return False
    return portfolio_scope_for_question(question) == PORTFOLIO_SCOPE
