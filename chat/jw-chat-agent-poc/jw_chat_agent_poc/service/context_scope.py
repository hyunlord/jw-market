from __future__ import annotations

import os
import re
from enum import StrEnum


class ContextScope(StrEnum):
    FILE = "FILE"
    MARKET = "MARKET"
    MIXED = "MIXED"


DEFAULT_FILE_REFERENCE_TERMS = (
    "이 보고서",
    "해당 보고서",
    "업로드 파일",
    "업로드한 파일",
    "첨부 파일",
    "첨부 문서",
    "이 문서",
    "해당 문서",
    "파일 값",
    "문서 결과",
    "PDF 결과",
    "엑셀",
)
_COMPARISON_RE = re.compile(
    r"(?:비교|대비|맞는지|차이|함께|결합)",
    re.IGNORECASE,
)


def file_reference_terms() -> tuple[str, ...]:
    configured = tuple(
        term.strip()
        for term in os.getenv("JW_CHAT_FILE_REFERENCE_TERMS", "").split(",")
        if term.strip()
    )
    return (*DEFAULT_FILE_REFERENCE_TERMS, *configured)


def has_file_reference(query: str) -> bool:
    normalized = re.sub(r"\s+", " ", query).strip().lower()
    return any(term.lower() in normalized for term in file_reference_terms())


def resolve_context_scope(
    query: str,
    *,
    has_active_file: bool,
    is_fresh_upload: bool = False,
    has_market_intent: bool = False,
    has_market_anchor: bool = False,
) -> ContextScope:
    """Resolve the request's data boundary before any market routing occurs."""

    file_directed = has_file_reference(query)
    if not has_active_file:
        if file_directed and has_market_intent and has_market_anchor and _COMPARISON_RE.search(query):
            return ContextScope.MIXED
        if file_directed:
            return ContextScope.FILE
        return ContextScope.MARKET
    if file_directed and has_market_intent and has_market_anchor and _COMPARISON_RE.search(query):
        return ContextScope.MIXED
    if not file_directed and has_market_intent and has_market_anchor:
        return ContextScope.MARKET
    # Fresh uploads and unresolved references remain in the file boundary.
    return ContextScope.FILE
