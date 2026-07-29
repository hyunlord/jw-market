from __future__ import annotations

import re
from typing import Final


_MARKET_NARRATIVE_TOKENS: Final[tuple[str, ...]] = (
    "추이",
    "추세",
    "경향",
    "변화",
    "흐름",
    "어때",
    "요즘 상황",
    "잘 나가",
    "잘돼",
    "잘 돼",
    "성장하나",
    "분석해줘",
    "분석해 줘",
    "경쟁구도",
    "경쟁 구도",
)

_MARKET_DATA_TOKENS: Final[tuple[str, ...]] = (
    "매출",
    "판매",
    "실적",
    "팔렸",
    "장사",
    "점유",
    "시장",
    "순위",
    "경쟁",
    "성장",
    "hhi",
    "cagr",
    "모멘텀",
    "momentum",
)

_SERIES_CONTEXT_TOKENS: Final[tuple[str, ...]] = (
    "월별",
    "시계열",
    "트렌드",
    "비교",
    "하락",
    "오르는",
    "동안",
    "위협",
)

_EXTERNAL_DATA_TOKENS: Final[tuple[str, ...]] = (
    "임상",
    "허가",
    "특허",
    "부작용",
    "성분",
    "질환",
    "질병",
    "환자",
    "뉴스",
    "이슈",
    "가이드라인",
)

_FILE_TOKENS: Final[tuple[str, ...]] = (
    "파일",
    "업로드",
    "첨부",
    "문서",
)

_DIRECT_VALUE_TOKENS: Final[tuple[str, ...]] = ("얼마", "몇", "수치", "값")
_EXPLICIT_PERIOD_RE: Final[re.Pattern[str]] = re.compile(
    r"20\d{2}(?:\s*년(?:\s*\d{1,2}\s*월|\s*[1-4]\s*분기)?|[-./]\d{1,2}|-?q[1-4])",
    re.IGNORECASE,
)


def _intent_flags(question: str) -> tuple[str, bool, bool, bool]:
    normalized = question.casefold()
    has_market_data = any(token.casefold() in normalized for token in _MARKET_DATA_TOKENS)
    has_narrative_cue = any(token.casefold() in normalized for token in _MARKET_NARRATIVE_TOKENS)
    has_external_data = any(token.casefold() in normalized for token in _EXTERNAL_DATA_TOKENS)
    return normalized, has_market_data, has_narrative_cue, has_external_data


def needs_market_series(question: str) -> bool:
    """Return whether answering the question requires market time-series evidence."""

    normalized, has_market_data, has_narrative_cue, has_external_data = _intent_flags(question)
    if any(token.casefold() in normalized for token in _FILE_TOKENS):
        return False
    if has_external_data and not has_market_data:
        return False
    has_series_context = has_market_data and any(
        token.casefold() in normalized for token in _SERIES_CONTEXT_TOKENS
    )
    return has_narrative_cue or has_series_context


def wants_market_narrative(question: str) -> bool:
    """Return whether verified market facts should be explained in natural prose."""

    normalized, has_market_data, has_narrative_cue, has_external_data = _intent_flags(question)
    if any(token.casefold() in normalized for token in _FILE_TOKENS):
        return False
    if has_external_data and not has_market_data:
        return False
    if (
        has_market_data
        and not has_narrative_cue
        and _EXPLICIT_PERIOD_RE.search(normalized)
        and any(token in normalized for token in _DIRECT_VALUE_TOKENS)
    ):
        return False
    return has_market_data or has_narrative_cue
