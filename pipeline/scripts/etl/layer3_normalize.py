#!/usr/bin/env python3
"""Period and numeric helpers for Layer 3 mart metric calculation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable


MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")


@dataclass(frozen=True)
class PeriodInfo:
    label: str
    kind: str
    year: int
    month: int | None = None
    quarter: int | None = None

    @property
    def sort_key(self) -> int:
        if self.kind == "month" and self.month is not None:
            return self.year * 12 + self.month
        if self.kind == "quarter" and self.quarter is not None:
            return self.year * 4 + self.quarter
        raise ValueError(f"invalid period info: {self}")


def parse_period(period: str) -> PeriodInfo:
    text = str(period).strip()
    month_match = MONTH_RE.match(text)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"invalid month period: {period!r}")
        return PeriodInfo(label=text, kind="month", year=year, month=month)

    quarter_match = QUARTER_RE.match(text)
    if quarter_match:
        return PeriodInfo(
            label=text,
            kind="quarter",
            year=int(quarter_match.group(1)),
            quarter=int(quarter_match.group(2)),
        )

    raise ValueError(f"unsupported period format: {period!r}")


def _shift_month(period: str, months: int) -> str | None:
    info = parse_period(period)
    if info.kind != "month" or info.month is None:
        return None
    zero_based = info.year * 12 + (info.month - 1) + months
    year, month_zero = divmod(zero_based, 12)
    return f"{year:04d}-{month_zero + 1:02d}"


def _shift_quarter(period: str, quarters: int) -> str | None:
    info = parse_period(period)
    if info.kind != "quarter" or info.quarter is None:
        return None
    zero_based = info.year * 4 + (info.quarter - 1) + quarters
    year, quarter_zero = divmod(zero_based, 4)
    return f"{year:04d}-Q{quarter_zero + 1}"


def compute_quarter(period_yyyymm: str) -> str:
    """Return a quarter label.

    Monthly periods map to the containing quarter. Quarterly periods are
    returned as-is.
    """
    info = parse_period(period_yyyymm)
    if info.kind == "quarter" and info.quarter is not None:
        return f"{info.year:04d}-Q{info.quarter}"
    if info.month is None:
        raise ValueError(f"month missing: {period_yyyymm!r}")
    return f"{info.year:04d}-Q{((info.month - 1) // 3) + 1}"


def prev_month(period_yyyymm: str) -> str | None:
    """Previous month for monthly periods; quarterly periods return None."""
    return _shift_month(period_yyyymm, -1)


def prev_quarter_month(period_yyyymm: str) -> str | None:
    """Previous quarter comparison period.

    For monthly data this means the same month in the previous quarter
    (minus three months). For quarterly data this means the previous quarter.
    """
    info = parse_period(period_yyyymm)
    if info.kind == "month":
        return _shift_month(period_yyyymm, -3)
    return _shift_quarter(period_yyyymm, -1)


def same_month_prev_year(period_yyyymm: str) -> str | None:
    """Same month/quarter in the previous year."""
    info = parse_period(period_yyyymm)
    if info.kind == "month":
        return _shift_month(period_yyyymm, -12)
    return _shift_quarter(period_yyyymm, -4)


def period_range_mat(period_yyyymm: str) -> list[str]:
    """Return the 12-month MAT window ending at period_yyyymm.

    MAT is defined only for monthly periods. Quarterly period labels return an
    empty list so callers can keep MAT NULL.
    """
    info = parse_period(period_yyyymm)
    if info.kind != "month":
        return []
    return [_shift_month(period_yyyymm, offset) for offset in range(-11, 1)]


def safe_div(a: float | int | None, b: float | int | None) -> float | None:
    if a is None or b is None:
        return None
    try:
        numerator = float(a)
        denominator = float(b)
    except (TypeError, ValueError):
        return None
    if math.isnan(numerator) or math.isnan(denominator) or denominator == 0:
        return None
    return numerator / denominator


def period_sort_key(period: str) -> int:
    return parse_period(period).sort_key


def is_monthly_period(period: str) -> bool:
    return parse_period(period).kind == "month"


def validate_periods(periods: Iterable[str]) -> None:
    for period in periods:
        parse_period(period)
