from __future__ import annotations

import re
from enum import StrEnum


class ContextScope(StrEnum):
    FILE = "FILE"
    MARKET = "MARKET"
    MIXED = "MIXED"


_MIXED_REFERENCE_RE = re.compile(
    r"(?:시장\s*(?:평균|데이터|전체)?\s*(?:과|와)?\s*(?:비교|대비)|시장과\s*비교|시장\s*기준으로\s*비교)",
    re.IGNORECASE,
)


def resolve_context_scope(
    query: str,
    *,
    has_active_file: bool,
    is_fresh_upload: bool = False,
    has_market_intent: bool = False,
) -> ContextScope:
    """Resolve the request's data boundary before any market routing occurs."""

    if not has_active_file:
        return ContextScope.MARKET
    if _MIXED_REFERENCE_RE.search(query):
        return ContextScope.MIXED
    # An active file owns ambiguous domain tokens such as brand and ATC4.
    # Market access is allowed only when the user explicitly asks to compare
    # the uploaded material with market data.
    return ContextScope.FILE
