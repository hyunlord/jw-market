from __future__ import annotations

import re


_PORTFOLIO_SCOPE_RE = re.compile(
    r"jw\s*(주요|전략|자사|전체)?\s*브랜드|(주요|전략|자사|전체)\s*브랜드|jw\s*포트폴리오|포트폴리오|자사\s*제품",
    flags=re.IGNORECASE,
)
_DECLINE_RE = re.compile(r"하락|감소|떨어|내려|잃|뺏|가져갔|가져갔는지|위축")
_MARKET_SHARE_RE = re.compile(r"시장\s*점유율|점유율|\bMS\b|market\s*share", flags=re.IGNORECASE)


def is_portfolio_decline_question(question: str) -> bool:
    """Detect conservative company/portfolio-scope market-share decline questions."""

    normalized = question.strip()
    if not normalized:
        return False
    if not _PORTFOLIO_SCOPE_RE.search(normalized):
        return False
    return bool(_DECLINE_RE.search(normalized) and _MARKET_SHARE_RE.search(normalized))
