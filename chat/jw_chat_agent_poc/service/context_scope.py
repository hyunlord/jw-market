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
    r"(?:비교|대비|맞는지|차이|함께|결합|합치|합쳐|같(?:아|은가|은지)|동일)",
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
# The document nouns a user actually types for an uploaded artefact. The set was
# 파일/문서/보고서/리포트 only, so "이 팩트시트에서", "이 가이드라인에서" read as
# naming no file at all and the document lane was never selected - the reported
# R68 2-F symptom. Every entry here is a document *kind*; deliberately generic
# words ("자료", "내용") are excluded because a market follow-up uses them for
# non-file material and would be re-routed away from mart.
_FILE_KIND_NOUNS = (
    r"파일|문서|보고서|리포트|레포트|팩트\s*시트|팩트시트|"
    r"가이드\s*라인|가이드라인|문건|첨부|슬라이드|엑셀|시트|pdf"
)
_QUALIFIED_FILE_REFERENCE_RE = re.compile(
    rf"[가-힣A-Za-z0-9_.\-]+\s*(?:{_FILE_KIND_NOUNS})\s*의",
    re.IGNORECASE,
)
_DEMONSTRATIVE_FILE_REFERENCE_RE = re.compile(
    rf"(?:이|그|해당)\s*(?:{_FILE_KIND_NOUNS})",
    re.IGNORECASE,
)
_FILE_LOCATION_REFERENCE_RE = re.compile(
    r"(?:어느|몇)\s*(?:시트|셀|페이지|쪽|표)|(?:시트|셀)\s*(?:이름|명|주소|좌표|위치)",
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
_EXPLICIT_HIRA_TERMS = (
    "심평원",
    "건강보험심사평가원",
    "hira",
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
        or _DEMONSTRATIVE_FILE_REFERENCE_RE.search(normalized)
    )


def has_file_axis_reference(query: str) -> bool:
    """Return whether the answer itself is requested from the active file."""

    return has_file_reference(query) or bool(_FILE_LOCATION_REFERENCE_RE.search(query))


def has_explicit_file_market_comparison(query: str) -> bool:
    """Return whether file and market evidence are both requested in one answer."""

    return bool(
        has_file_axis_reference(query)
        and _has_explicit_market_reference(query)
        and _COMPARISON_RE.search(query)
    )


def explicit_file_comparison_sources(query: str) -> tuple[str, ...]:
    """Return explicitly named non-file lanes for a file comparison request."""

    if not has_file_axis_reference(query) or not _COMPARISON_RE.search(query):
        return ()
    normalized = re.sub(r"\s+", " ", query).strip().casefold()
    sources: list[str] = []
    if _has_explicit_market_reference(normalized):
        sources.append("mart")
    if any(term in normalized for term in _EXPLICIT_HIRA_TERMS):
        sources.append("hira")
    return tuple(sources)


def has_explicit_file_source_comparison(query: str) -> bool:
    """Return whether the request explicitly asks to compare file and source facts."""

    return bool(explicit_file_comparison_sources(query))


def file_comparison_leg_query(query: str) -> str:
    """Return the file-only leg without changing the original planner question."""

    comparison_sources = explicit_file_comparison_sources(query)
    if not comparison_sources:
        return query
    clauses = re.split(r"(?:와|과)\s+", query, maxsplit=1)
    if len(clauses) != 2:
        return query
    source_terms = (*_EXPLICIT_MARKET_TERMS, *_EXPLICIT_HIRA_TERMS)
    source_pattern = re.compile(
        "|".join(re.escape(term) for term in sorted(source_terms, key=len, reverse=True)),
        re.IGNORECASE,
    )
    left, right = (clause.strip() for clause in clauses)
    if has_file_axis_reference(left) and source_pattern.search(right):
        file_part, source_part = left, right
    elif source_pattern.search(left) and has_file_axis_reference(right):
        file_part, source_part = right, left
    else:
        return query

    file_part = re.sub(
        r"(?:을|를)?\s*(?:비교|대조|같(?:아|은가|은지)|동일).*$",
        "",
        file_part,
        flags=re.IGNORECASE,
    ).strip()
    fusion = re.search(
        r"(?:합쳐서|합치고|함께)\s*(?P<shared>.+)$",
        source_part,
        re.IGNORECASE,
    )
    shared = fusion.group("shared").strip() if fusion else ""
    if not shared and "mart" in comparison_sources:
        period = re.search(
            r"20\d{2}(?:\s*년\s*(?:0?[1-9]|1[0-2])\s*월|[-./](?:0?[1-9]|1[0-2]))",
            source_part,
        )
        metric = re.search(r"(?:sell\s*out|총액|매출|유병률|환자\s*수)", source_part, re.IGNORECASE)
        shared = " ".join(
            value.group(0) for value in (period, metric) if value is not None
        )
    if shared:
        if re.search(r"(?:이|그|해당|업로드한)\s*파일$", file_part):
            file_part = f"{file_part}에서"
        shared = re.sub(r"(?:을|를)?\s*(?:비교|대조).*$", "", shared).strip()
        file_query = re.sub(r"\s+", " ", f"{file_part} {shared}").strip()
        if not file_query.endswith(("알려줘", "알려주세요")):
            file_query = f"{file_query} 알려줘"
        return file_query
    return f"{file_part} 알려줘"


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
