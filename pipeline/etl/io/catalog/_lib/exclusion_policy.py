"""Sheet-level exclusion semantics for MI Master strategic markets.

The MI Master uses the same visible marker ("제외") with different meanings
per sheet.  Column-position heuristics are not reliable enough because some
row-exclusion sheets put the marker in Class-like columns, while class-only
exclusion sheets must keep the row in the market denominator.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

ExclusionMode = Literal["row", "class", "dimension_na", "none", "legacy"]

ROW_EXCLUSION_MARKETS = frozenset({"strategy_006", "strategy_007", "strategy_009"})
CLASS_EXCLUSION_MARKETS = frozenset({"strategy_013", "strategy_016"})
DIMENSION_NA_EXCLUSION_MARKETS = frozenset({"strategy_014"})
NO_EXCLUSION_MARKETS = frozenset({
    "strategy_001",
    "strategy_002",
    "strategy_003",
    "strategy_004",
    "strategy_005",
    "strategy_008",
    "strategy_010",
    "strategy_011",
    "strategy_012",
    "strategy_015",
})

EXCLUSION_MODE_BY_MARKET_ID: dict[str, ExclusionMode] = {
    **{market_id: "row" for market_id in ROW_EXCLUSION_MARKETS},
    **{market_id: "class" for market_id in CLASS_EXCLUSION_MARKETS},
    **{market_id: "dimension_na" for market_id in DIMENSION_NA_EXCLUSION_MARKETS},
    **{market_id: "none" for market_id in NO_EXCLUSION_MARKETS},
}

EXCLUSION_MODE_BY_SHEET_NAME: dict[str, ExclusionMode] = {
    "리바로 리바로젯": "row",
    "리바로페노": "row",
    "트루패스 피나스타 제이다트": "row",
    "헴리브라": "class",
    "플라주오피": "class",
    "위너프 위너프A+": "dimension_na",
    "위너프 위너프에이플러스": "dimension_na",
    "라베칸 라베칸듀오": "none",
    "제이클": "none",
    "가드렛 가드메트": "none",
    "타발리스": "none",
    "시그마트": "none",
    "리바로하이 리바로브이": "none",
    "뉴트로진 모빌리아": "none",
    "악템라": "none",
    "페린젝트 베노훼럼": "none",
    "엔커버": "none",
}


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    return text.replace("위너프A+", "위너프에이플러스")


def contains_exclusion_marker(value: Any) -> bool:
    text = clean_text(value)
    return bool(text and "제외" in text and not text.startswith("비제외"))


def _normalize_sheet_name(value: Any) -> str | None:
    text = clean_text(value)
    return re.sub(r"\s+", " ", text) if text else None


def exclusion_mode_for_market(
    *,
    strategic_market_id: str | None = None,
    sheet_name: str | None = None,
    default: ExclusionMode = "legacy",
) -> ExclusionMode:
    market_id = str(strategic_market_id or "").strip()
    if market_id in EXCLUSION_MODE_BY_MARKET_ID:
        return EXCLUSION_MODE_BY_MARKET_ID[market_id]

    normalized_sheet = _normalize_sheet_name(sheet_name)
    if normalized_sheet and normalized_sheet in EXCLUSION_MODE_BY_SHEET_NAME:
        return EXCLUSION_MODE_BY_SHEET_NAME[normalized_sheet]
    return default


def classify_exclusion_cells(
    values: list[Any] | tuple[Any, ...],
    *,
    class_indexes: set[int] | None = None,
    strategic_market_id: str | None = None,
    sheet_name: str | None = None,
    default: ExclusionMode = "legacy",
) -> tuple[bool, bool]:
    """Return (row_excluded, class_excluded) using sheet-level semantics."""

    marker_indexes = [idx for idx, value in enumerate(values) if contains_exclusion_marker(value)]
    if not marker_indexes:
        return False, False

    mode = exclusion_mode_for_market(
        strategic_market_id=strategic_market_id,
        sheet_name=sheet_name,
        default=default,
    )
    if mode == "row":
        return True, False
    if mode == "class":
        return False, True
    if mode in {"dimension_na", "none"}:
        return False, False

    class_indexes = set(class_indexes or ())
    row_excluded = False
    class_excluded = False
    for idx in marker_indexes:
        if idx in class_indexes:
            class_excluded = True
        else:
            row_excluded = True
    return row_excluded, class_excluded
