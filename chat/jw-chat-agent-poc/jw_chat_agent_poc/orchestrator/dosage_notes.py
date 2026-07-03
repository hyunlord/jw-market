from __future__ import annotations

from collections.abc import Iterable


DOSAGE_COMBINATION_NOTE_PREFIX = "※ 본 시장의 제형 구분은 성분 조합 기준"

_PHYSICAL_DOSAGE_TERMS = (
    "정",
    "정제",
    "캡슐",
    "캡슐제",
    "주",
    "주사",
    "주사제",
    "시럽",
    "시럽제",
    "액",
    "액제",
    "현탁",
    "현탁액",
    "과립",
    "과립제",
    "산제",
    "분말",
    "크림",
    "연고",
    "패치",
    "점안",
    "점안액",
    "흡입",
    "흡입제",
    "서방",
    "구강붕해",
    "복합정",
)

_COMBINATION_SEGMENT_MARKERS = ("/", "+", "·")


def dosage_combination_note(axis_label: str, values: Iterable[object]) -> str:
    """Return a dosage-axis footnote when values look like ingredient-combination segments."""

    if axis_label.strip() != "제형":
        return ""
    examples = _distinct_values(values)
    if len(examples) < 2:
        return ""
    if _looks_like_physical_dosage_values(examples):
        return ""
    if not any(_looks_like_combination_segment(value) for value in examples):
        return ""
    return f"{DOSAGE_COMBINATION_NOTE_PREFIX}(예: {examples[0]} vs {examples[1]})입니다."


def is_dosage_combination_note(line: str) -> bool:
    return line.strip().startswith(DOSAGE_COMBINATION_NOTE_PREFIX)


def _distinct_values(values: Iterable[object]) -> tuple[str, ...]:
    distinct: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        distinct.append(text)
        if len(distinct) >= 2:
            break
    return tuple(distinct)


def _looks_like_physical_dosage_values(values: tuple[str, ...]) -> bool:
    return all(_looks_like_physical_dosage_value(value) for value in values)


def _looks_like_physical_dosage_value(value: str) -> bool:
    compact = value.replace(" ", "")
    return compact in _PHYSICAL_DOSAGE_TERMS or any(compact.endswith(term) for term in _PHYSICAL_DOSAGE_TERMS)


def _looks_like_combination_segment(value: str) -> bool:
    return any(marker in value for marker in _COMBINATION_SEGMENT_MARKERS)
