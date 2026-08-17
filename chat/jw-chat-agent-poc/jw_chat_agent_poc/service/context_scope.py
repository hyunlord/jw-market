from __future__ import annotations

import os
import re
from collections.abc import Sequence
from enum import StrEnum


class ContextScope(StrEnum):
    FILE = "FILE"
    MARKET = "MARKET"
    MIXED = "MIXED"


DEFAULT_FILE_REFERENCE_TERMS = (
    "이 보고서",
    "해당 보고서",
    "업로드 파일",
    "업로드",
    "업로드한 파일",
    "업로드한",
    "업로드된",
    "첨부파일",
    "첨부 파일",
    "첨부한",
    "첨부 문서",
    "이 문서",
    "해당 문서",
    "이 셀아웃",
    "파일 값",
    "문서 결과",
    "PDF 결과",
    "pdf",
    "ppt",
    "파워포인트",
    "word",
    "워드",
    "엑셀",
)
_COMPARISON_RE = re.compile(
    r"(?:비교|대비|맞는지|차이|함께|결합)",
    re.IGNORECASE,
)
_ATC4_CODE_RE = re.compile(r"(?<![A-Z0-9])[A-Z]\d{2}[A-Z]\d(?![A-Z0-9])", re.IGNORECASE)
_PLURAL_FILE_REFERENCE_RE = re.compile(
    r"(?:두|여러)\s+(?:[가-힣A-Za-z0-9_-]+\s+){0,3}(?:보고서|문서|파일)",
    re.IGNORECASE,
)
# A file named by its own title rather than by a demonstrative: "CHSO 문서의",
# "3월 보고서의". DEFAULT_FILE_REFERENCE_TERMS only covers demonstratives
# ("이 문서", "해당 보고서"), so a question that says which file it means was
# read as naming no file at all.
#
# Deliberately limited to the genitive "의". Wider particles ("파일에 있는")
# also re-route a corpus case frozen in routing_inputs.v3.json, a write-once
# pre-cutover characterization asset owned by another phase. Widening the
# particle set is a separate decision, not a side effect of this one.
_QUALIFIED_FILE_REFERENCE_RE = re.compile(
    r"[가-힣A-Za-z0-9_.\-]+\s*(?:보고서|문서|파일|엑셀|시트)\s*의",
    re.IGNORECASE,
)
_EXPLICIT_MARKET_TERMS = (
    "시장 데이터",
    "전체 시장",
    "시장 기준",
    "db에서",
    "mart에서",
    "ubist",
    "iqvia",
    # Korean names for the same internal source. Without these a comparison
    # request reads as market-ungrounded and never reaches MIXED.
    "마트",
    "내부 데이터",
    "자사 데이터",
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
    return bool(
        any(term.lower() in normalized for term in file_reference_terms())
        or _PLURAL_FILE_REFERENCE_RE.search(normalized)
        or _QUALIFIED_FILE_REFERENCE_RE.search(normalized)
    )


def resolve_context_scope(
    query: str,
    *,
    has_active_file: bool,
    is_fresh_upload: bool = False,
    has_market_intent: bool = False,
    has_market_anchor: bool = False,
    file_schema_columns: Sequence[str] = (),
) -> ContextScope:
    """Resolve the request's data boundary before any market routing occurs."""

    file_directed = has_file_reference(query)
    explicit_market = _has_explicit_market_reference(query)
    market_grounded = has_market_anchor or explicit_market
    schema_directed = matches_file_schema(query, file_schema_columns)
    if not has_active_file:
        if file_directed and has_market_intent and market_grounded and _COMPARISON_RE.search(query):
            return ContextScope.MIXED
        if file_directed:
            return ContextScope.FILE
        return ContextScope.MARKET
    # An uploaded file is one evidence source among peers. Every non-empty turn
    # in that session therefore keeps both the file and market legs observable;
    # the answer assembler decides ordering and density, not whether a leg runs.
    if query.strip():
        return ContextScope.MIXED
    # A fresh upload with no question only needs the file-ready response.
    return ContextScope.FILE


def _has_explicit_market_reference(query: str) -> bool:
    normalized = re.sub(r"\s+", " ", query).strip().casefold()
    return any(term in normalized for term in _EXPLICIT_MARKET_TERMS)


def matches_file_schema(query: str, columns: Sequence[str]) -> bool:
    if not columns:
        return False
    normalized_columns = tuple(
        re.sub(r"[^a-z0-9가-힣]+", "", column.casefold())
        for column in columns
    )
    has_atc4_axis = any("atc4" in column for column in normalized_columns)
    has_manufacturer_axis = any(
        any(term in column for term in ("mfr", "manufacturer", "제조사", "업체"))
        for column in normalized_columns
    )
    has_channel_axis = any(
        any(term in column for term in ("channel", "채널"))
        for column in normalized_columns
    )
    has_product_axis = any(
        any(term in column for term in ("product", "제품", "품목"))
        for column in normalized_columns
    )
    if has_atc4_axis and _ATC4_CODE_RE.search(query):
        return True
    if has_channel_axis and "채널" in query:
        return True
    if has_product_axis and any(term in query for term in ("제품", "품목")):
        return True
    return bool(
        has_manufacturer_axis
        and _COMPARISON_RE.search(query)
        and re.search(r"[가-힣A-Za-z0-9]+(?:과|와)\s*[가-힣A-Za-z0-9]+", query)
    )
