"""Period parsing helpers for strategy market-scope recomputation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import TypeVar


T = TypeVar("T")

_MONTH_PERIOD_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
_QUARTER_PERIOD_RE = re.compile(r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$")


class PeriodFormatError(Exception):
    """Raised when a strategy period is neither ``YYYY-MM`` nor ``YYYY-Qn``."""

    def __init__(self, period: str) -> None:
        """Store the unsupported period for stable diagnostics."""

        self.period = period
        super().__init__(f"unsupported strategy period format: {period}")


@dataclass(frozen=True, slots=True)
class PeriodPoint:
    """A strategy period represented on a month ordinal timeline."""

    month_ordinal: int


def sort_periods(periods: Iterable[str]) -> tuple[str, ...]:
    """Return periods in chronological order for monthly or quarterly keys."""

    return tuple(sorted(periods, key=lambda period: _parse_period(period).month_ordinal))


def sorted_period_items(values: Mapping[str, T]) -> dict[str, T]:
    """Return a dict ordered by chronological strategy period keys."""

    return {period: values[period] for period in sort_periods(values)}


def period_span_years(start: str, end: str) -> float:
    """Return elapsed years for monthly or quarterly strategy periods.

    Quarterly periods are placed on the first month of each quarter, so a
    20-quarter IQVIA span is represented as 60 elapsed months, equivalent to
    ``20 / 4`` years. Monthly UBIST spans keep the existing ``months / 12``
    rule.
    """

    start_point = _parse_period(start)
    end_point = _parse_period(end)
    return (end_point.month_ordinal - start_point.month_ordinal) / 12


def _parse_period(period: str) -> PeriodPoint:
    """Parse one supported strategy period into a sortable month ordinal."""

    month_match = _MONTH_PERIOD_RE.fullmatch(period)
    if month_match:
        year = int(month_match.group("year"))
        month = int(month_match.group("month"))
        return PeriodPoint(month_ordinal=(year * 12) + month)

    quarter_match = _QUARTER_PERIOD_RE.fullmatch(period)
    if quarter_match:
        year = int(quarter_match.group("year"))
        quarter = int(quarter_match.group("quarter"))
        quarter_start_month = ((quarter - 1) * 3) + 1
        return PeriodPoint(month_ordinal=(year * 12) + quarter_start_month)

    raise PeriodFormatError(period)
