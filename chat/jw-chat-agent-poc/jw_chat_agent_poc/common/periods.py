from __future__ import annotations

import re
from typing import Final


_YEAR_MONTH_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?<!\d)(?P<year>20\d{2}|\d{2})\s*년\s*(?P<month>1[0-2]|0?[1-9])\s*월"
    ),
    re.compile(
        r"(?<!\d)(?P<year>20\d{2}|\d{2})\s*[-./]\s*(?P<month>1[0-2]|0?[1-9])(?!\d)"
    ),
    re.compile(r"(?<!\d)(?P<month>1[0-2]|0?[1-9])\s*/\s*(?P<year>20\d{2})(?!\d)"),
)
_YEAR_QUARTER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?<!\d)(?P<year>20\d{2}|\d{2})\s*년?\s*(?P<quarter>[1-4])\s*분기"
    ),
    re.compile(
        r"(?<!\d)(?P<year>20\d{2}|\d{2})\s*-?\s*Q(?P<quarter>[1-4])(?!\d)",
        re.IGNORECASE,
    ),
)
_EXPLICIT_PERIOD_CUE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?:20\d{2}|\d{2})\s*(?:년\s*\d{1,2}\s*(?:월|분기)|[-./]\s*\d{1,2}|-?\s*Q\d)"
    r"|(?<!\d)\d{1,2}\s*/\s*20\d{2}(?!\d)",
    re.IGNORECASE,
)
_YEAR_ONLY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?P<year>20\d{2}|\d{2})\s*년"
    r"(?!\s*(?:[1-4]\s*분기|(?:1[0-2]|0?[1-9])\s*월))"
)
_RELATIVE_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"최근\s*(?P<count>\d{1,2})\s*(?P<unit>년|개월|달)"
)


def canonical_periods(text: str) -> tuple[str, ...]:
    """Return explicit month and quarter references in canonical textual order."""
    matches: list[tuple[int, int, str]] = []
    for pattern in _YEAR_MONTH_PATTERNS:
        for match in pattern.finditer(text):
            year = _four_digit_year(match.group("year"))
            month = int(match.group("month"))
            matches.append((match.start(), match.end(), f"{year:04d}-{month:02d}"))
    for pattern in _YEAR_QUARTER_PATTERNS:
        for match in pattern.finditer(text):
            year = _four_digit_year(match.group("year"))
            matches.append(
                (match.start(), match.end(), f"{year:04d}-Q{match.group('quarter')}")
            )

    periods: list[str] = []
    occupied: list[tuple[int, int]] = []
    ordered_matches = sorted(matches, key=lambda item: (item[0], -(item[1] - item[0])))
    for start, end, period in ordered_matches:
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        if period not in periods:
            periods.append(period)
    return tuple(periods)


def month_keys(text: str) -> frozenset[str]:
    """Return canonical month references while excluding quarter references."""
    return frozenset(period for period in canonical_periods(text) if "-Q" not in period)


def has_explicit_period_cue(text: str) -> bool:
    """Return whether the text contains an explicit period-like expression."""
    return _EXPLICIT_PERIOD_CUE.search(text) is not None


def first_explicit_period_cue(text: str) -> str:
    """Return the first explicit period-like fragment for fail-closed reporting."""
    match = _EXPLICIT_PERIOD_CUE.search(text)
    return " ".join(match.group(0).split()) if match is not None else ""


def requested_period(text: str) -> str | None:
    """Return a normalized explicit period constraint when one is present."""
    periods = canonical_periods(text)
    if periods:
        return periods[0]
    if has_explicit_period_cue(text):
        return first_explicit_period_cue(text)
    year_match = _YEAR_ONLY_PATTERN.search(text)
    if year_match is not None:
        return str(_four_digit_year(year_match.group("year")))
    relative_match = _RELATIVE_RANGE_PATTERN.search(text)
    if relative_match is not None:
        unit = "개월" if relative_match.group("unit") == "달" else relative_match.group("unit")
        return f"최근 {int(relative_match.group('count'))}{unit}"
    return None


def _four_digit_year(raw_year: str) -> int:
    year = int(raw_year)
    return year if year >= 100 else 2000 + year
