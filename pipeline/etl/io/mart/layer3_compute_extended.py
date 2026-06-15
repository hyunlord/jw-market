from __future__ import annotations

import math
from typing import Any, Iterable

import pandas as pd

from .layer3_normalize import parse_period, period_sort_key, safe_div

GROWTH_CONTRIBUTION_THRESHOLD = 10_000.0
EI_DENOMINATOR_THRESHOLD = 0.0
GC_SMALL_DENOMINATOR_WARNING = "gc_small_denominator"
EI_SMALL_DENOMINATOR_WARNING = "ei_small_denominator"

def period_kind_and_ord(period: str) -> tuple[str, int]:
    info = parse_period(str(period))
    if info.kind == "month" and info.month is not None:
        return "month", info.year * 12 + info.month
    if info.kind == "quarter" and info.quarter is not None:
        return "quarter", info.year * 4 + info.quarter
    raise ValueError(f"unsupported period: {period!r}")

def periods_per_year(period_kind: str) -> int:
    if period_kind == "month":
        return 12
    if period_kind == "quarter":
        return 4
    raise ValueError(f"unsupported period kind: {period_kind!r}")

def safe_number(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
    except (TypeError, ValueError):
        return None
    return number

def safe_ratio(numerator: Any, denominator: Any) -> float | None:
    denominator_f = safe_number(denominator)
    if denominator_f is None or denominator_f == 0:
        return None
    numerator_f = safe_number(numerator)
    if numerator_f is None:
        return None
    return numerator_f / denominator_f

def compute_cagr_value(end_value: Any, start_value: Any, years: int) -> float | None:
    ratio = safe_ratio(end_value, start_value)
    if ratio is None or ratio < 0:
        return None
    return (ratio ** (1 / years)) - 1

def compute_ei(brand_cagr_5y: Any, market_cagr_5y: Any) -> tuple[float | None, str | None]:
    denominator_f = safe_number(market_cagr_5y)
    if denominator_f is not None and abs(denominator_f) <= EI_DENOMINATOR_THRESHOLD:
        return None, EI_SMALL_DENOMINATOR_WARNING
    ratio = safe_ratio(brand_cagr_5y, market_cagr_5y)
    if ratio is None:
        return None, None
    return ratio * 100, None

def compute_growth_contribution(brand_growth_abs: Any, market_growth_abs: Any) -> tuple[float | None, str | None]:
    denominator_f = safe_number(market_growth_abs)
    if denominator_f is not None and abs(denominator_f) <= GROWTH_CONTRIBUTION_THRESHOLD:
        return None, GC_SMALL_DENOMINATOR_WARNING
    ratio = safe_ratio(brand_growth_abs, market_growth_abs)
    if ratio is None:
        return None, None
    return ratio * 100, None

def compute_hhi(brand_ms_list: Iterable[Any]) -> float | None:
    values: list[float] = []
    for value in brand_ms_list:
        if value is None or pd.isna(value):
            continue
        values.append(float(value))
    if not values:
        return None
    return sum((ms * 100) ** 2 for ms in values)

def compute_momentum(quarterly_ms_percent: list[float]) -> float | None:
    if len(quarterly_ms_percent) < 4 or any(value is None or pd.isna(value) for value in quarterly_ms_percent):
        return None
    xs = [1, 2, 3, 4]
    ys = [float(value) for value in quarterly_ms_percent[-4:]]
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=False))
    sum_y = sum(ys)
    return (4 * sum_xy - 10 * sum_y) / 20
