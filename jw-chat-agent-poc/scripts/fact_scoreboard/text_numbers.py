from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal


NumericUnit = Literal["percent", "eok", "rank", "count", "plain"]

_NUMERIC_PATTERN: Final = re.compile(
    r"(?<![0-9])(?P<sign>[-−])?\s*(?P<number>[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.(?P<decimal>[0-9]+))?\s*(?P<unit>억원|%|위|명|개)?"
)
_PERIOD_PATTERN: Final = re.compile(r"^[0-9]{4}$|^[0-9]{1,2}$")


@dataclass(frozen=True, slots=True)
class NumericMention:
    """One material numeric value extracted from a Korean markdown answer."""

    raw: str
    value: float
    unit: NumericUnit
    context: str


def extract_numeric_mentions(answer: str) -> tuple[NumericMention, ...]:
    """Extract metric-like numeric mentions while ignoring date fragments."""

    mentions: list[NumericMention] = []
    for match in _NUMERIC_PATTERN.finditer(answer):
        unit = _unit(match.group("unit"), answer, match.start(), match.end())
        if unit is None:
            continue
        number = match.group("number").replace(",", "")
        decimal = match.group("decimal")
        raw_number = number if decimal is None else f"{number}.{decimal}"
        sign = -1.0 if match.group("sign") in {"-", "−"} else 1.0
        if _looks_like_period(raw_number, answer, match.start(), match.end(), unit):
            continue
        raw = match.group(0).replace(" ", "")
        if unit == "rank":
            sign = 1.0
            raw = raw.lstrip("-−")
        mentions.append(
            NumericMention(
                raw=raw,
                value=float(raw_number) * sign,
                unit=unit,
                context=_context(answer, match.start(), match.end()),
            )
        )
    return tuple(mentions)


def _unit(raw_unit: str | None, text: str, start: int, end: int) -> NumericUnit | None:
    match raw_unit:
        case "%":
            return "percent"
        case "억원":
            return "eok"
        case "위":
            return "rank"
        case "명" | "개":
            return "count"
        case None:
            window = text[max(0, start - 12) : min(len(text), end + 12)]
            if "HHI" in window or "집중도" in window:
                return "plain"
            return None
        case unreachable:
            raise AssertionError(f"unreachable numeric unit: {unreachable}")


def _looks_like_period(raw_number: str, text: str, start: int, end: int, unit: NumericUnit) -> bool:
    if unit != "plain" and unit != "count":
        return False
    before = text[max(0, start - 1) : start]
    after = text[end : min(len(text), end + 1)]
    return bool(_PERIOD_PATTERN.match(raw_number) and (after in {"년", "월"} or before == "-"))


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - 18) : min(len(text), end + 18)].replace("\n", " ").strip()
