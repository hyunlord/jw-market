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
_COMPOUND_RELATIVE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"최근\s*(?P<years>\d{1,2})\s*년\s*(?P<months>\d{1,2})\s*(?:개월|달)"
)
_HALF_YEAR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"최근\s*(?P<years>\d{1,2})\s*년\s*반"
)
_RECENT_QUARTER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"최근\s*(?P<quarters>\d{1,2})\s*분기"
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


def explicit_years(text: str) -> tuple[int, ...]:
    """Return bare year references that carry no month or quarter of their own.

    ``canonical_periods`` deliberately ignores these because a year is not a
    single period key. Callers that can expand a year into its months need them
    separately, so the span a question asks for is never silently dropped.
    """
    years: list[int] = []
    for match in _YEAR_ONLY_PATTERN.finditer(text):
        year = _four_digit_year(match.group("year"))
        if year not in years:
            years.append(year)
    return tuple(years)


def quarter_keys(text: str) -> frozenset[str]:
    """Return canonical quarter references, the complement of ``month_keys``."""
    return frozenset(period for period in canonical_periods(text) if "-Q" in period)


def relative_span(text: str) -> tuple[int, str] | None:
    """Return a ``(count, unit)`` pair for expressions such as ``최근 3년``."""
    compound = _COMPOUND_RELATIVE_PATTERN.search(text)
    if compound is not None:
        months = int(compound.group("years")) * 12 + int(compound.group("months"))
        return months, "개월"
    half_year = _HALF_YEAR_PATTERN.search(text)
    if half_year is not None:
        return int(half_year.group("years")) * 12 + 6, "개월"
    quarters = _RECENT_QUARTER_PATTERN.search(text)
    if quarters is not None:
        return int(quarters.group("quarters")) * 3, "개월"
    match = _RELATIVE_RANGE_PATTERN.search(text)
    if match is None:
        return None
    unit = "개월" if match.group("unit") == "달" else match.group("unit")
    return int(match.group("count")), unit


def requested_month_range(text: str, anchor: str) -> tuple[str, str, int] | None:
    """Return the requested inclusive month range and its exact month count."""
    explicit = tuple(period for period in canonical_periods(text) if "-Q" not in period)
    if len(explicit) >= 2 and re.search(r"(?:부터|까지|~|～|[-–—])", text):
        start, end = explicit[0], explicit[-1]
        count = _inclusive_month_count(start, end)
        return (start, end, count) if count > 0 else None

    span = relative_span(text)
    if span is None:
        return None
    count, unit = span
    months = count * 12 if unit == "년" else count
    periods = months_back(anchor, months)
    if not periods:
        return None
    return periods[0], periods[-1], len(periods)


def year_months(year: int) -> tuple[str, ...]:
    """Return the twelve canonical month keys of ``year`` in ascending order."""
    return tuple(f"{year:04d}-{month:02d}" for month in range(1, 13))


def quarter_months(quarter_key: str) -> tuple[str, ...]:
    """Return the three canonical month keys covered by ``YYYY-Qn``."""
    year_text, _, quarter_text = quarter_key.partition("-Q")
    try:
        year = int(year_text)
        quarter = int(quarter_text)
    except ValueError:
        return ()
    if not 1 <= quarter <= 4:
        return ()
    first = (quarter - 1) * 3 + 1
    return tuple(f"{year:04d}-{month:02d}" for month in range(first, first + 3))


def months_back(anchor: str, count: int) -> tuple[str, ...]:
    """Return ``count`` month keys ending at ``anchor`` inclusive, ascending."""
    if count <= 0:
        return ()
    try:
        year_text, month_text = anchor.split("-", 1)
        year = int(year_text)
        month = int(month_text)
    except (ValueError, TypeError):
        return ()
    if not 1 <= month <= 12:
        return ()
    index = year * 12 + (month - 1)
    start = max(index - (count - 1), 0)
    return tuple(
        f"{value // 12:04d}-{value % 12 + 1:02d}" for value in range(start, index + 1)
    )


def _inclusive_month_count(start: str, end: str) -> int:
    try:
        start_year, start_month = (int(value) for value in start.split("-", 1))
        end_year, end_month = (int(value) for value in end.split("-", 1))
    except (TypeError, ValueError):
        return 0
    start_index = start_year * 12 + start_month - 1
    end_index = end_year * 12 + end_month - 1
    return max(0, end_index - start_index + 1)


def _four_digit_year(raw_year: str) -> int:
    year = int(raw_year)
    return year if year >= 100 else 2000 + year
