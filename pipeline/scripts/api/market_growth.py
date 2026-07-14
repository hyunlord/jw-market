"""Growth calculations shared by cause response paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class GrowthResult:
    value: float | None
    reason: str | None
    baseline_period: str | None
    period_count: int


def compound_period_growth_pct(
    previous: float | None,
    current: float | None,
    periods_per_year: int,
) -> float | None:
    """Return compound growth for a caller-provided fixed period count."""

    if previous is None or previous <= 0 or current is None or current < 0:
        return None
    return (math.pow(current / previous, 1 / periods_per_year) - 1) * 100


def fixed_five_year_growth_series(
    values_by_period: Mapping[str, float | None],
    *,
    source: str | None = None,
) -> dict[str, GrowthResult]:
    """Return fixed-scale CQGR/CMGR values without shortening the exponent."""

    periods = sorted(str(period) for period in values_by_period)
    period_count = _fixed_period_count(source, periods)
    if not periods:
        return {}

    earliest = periods[0]
    results: dict[str, GrowthResult] = {}
    for index, period in enumerate(periods):
        if index == 0:
            results[period] = GrowthResult(None, "insufficient_history", None, period_count)
            continue

        exact_baseline = _five_year_prior_period(period)
        baseline_period = exact_baseline if exact_baseline in values_by_period else earliest
        baseline = _number(values_by_period.get(baseline_period))
        current = _number(values_by_period.get(period))
        if baseline is None or current is None:
            results[period] = GrowthResult(None, "insufficient_history", baseline_period, period_count)
        elif baseline == 0:
            results[period] = GrowthResult(None, "zero_baseline", baseline_period, period_count)
        elif baseline < 0 or current < 0:
            results[period] = GrowthResult(None, "invalid_baseline", baseline_period, period_count)
        else:
            value = compound_period_growth_pct(baseline, current, period_count)
            results[period] = GrowthResult(value, None, baseline_period, period_count)
    return results


def _fixed_period_count(source: str | None, periods: list[str]) -> int:
    source_key = str(source or "").strip().lower()
    if source_key in {"ubist"}:
        return 60
    if source_key in {"iqvia", "iqvia_nsa"}:
        return 20
    return 20 if periods and all("-Q" in period for period in periods) else 60


def _five_year_prior_period(period: str) -> str:
    year, suffix = period.split("-", 1)
    return f"{int(year) - 5}-{suffix}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
