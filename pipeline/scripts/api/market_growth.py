"""Growth calculations shared by cause response paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any


GROWTH_POINT_KEYS: tuple[str, ...] = ("mom_growth_pct", "cmgr", "cqgr")


@dataclass(frozen=True, slots=True)
class GrowthResult:
    value: float | None
    reason: str | None
    baseline_period: str | None
    period_count: int


def compound_period_growth_pct(
    previous: float | None,
    current: float | None,
    elapsed_periods: int,
    *,
    periods_per_year: int,
) -> float | None:
    """Return annualized compound growth over the actual elapsed periods."""

    if previous is None or previous <= 0 or current is None or current < 0 or elapsed_periods <= 0:
        return None
    return (math.pow(current / previous, periods_per_year / elapsed_periods) - 1) * 100


def fixed_five_year_growth_series(
    values_by_period: Mapping[str, float | None],
    *,
    source: str | None = None,
) -> dict[str, GrowthResult]:
    """Return CQGR/CMGR values against one baseline fixed for the range."""

    periods = sorted(str(period) for period in values_by_period)
    if not periods:
        return {}

    numeric_periods = [period for period in periods if _number(values_by_period.get(period)) is not None]
    if not numeric_periods:
        return {
            period: GrowthResult(None, "insufficient_history", None, 0)
            for period in periods
        }

    latest_period = numeric_periods[-1]
    exact_baseline = _five_year_prior_period(latest_period)
    baseline_period = exact_baseline if exact_baseline in numeric_periods else numeric_periods[0]
    baseline = _number(values_by_period.get(baseline_period))
    periods_per_year = _periods_per_year(source, periods)

    results: dict[str, GrowthResult] = {}
    for period in periods:
        elapsed_periods = max(0, _elapsed_periods(baseline_period, period, periods_per_year))
        current = _number(values_by_period.get(period))
        if baseline is None or current is None or elapsed_periods == 0:
            results[period] = GrowthResult(None, "insufficient_history", baseline_period, elapsed_periods)
        elif baseline == 0:
            results[period] = GrowthResult(None, "zero_baseline", baseline_period, elapsed_periods)
        elif baseline < 0 or current < 0:
            results[period] = GrowthResult(None, "invalid_baseline", baseline_period, elapsed_periods)
        else:
            value = compound_period_growth_pct(
                baseline,
                current,
                elapsed_periods,
                periods_per_year=periods_per_year,
            )
            results[period] = GrowthResult(value, None, baseline_period, elapsed_periods)
    return results


def growth_endpoint_meta(values_by_period: Mapping[str, float | None]) -> dict[str, str | None]:
    """Describe the latest real value used as the growth endpoint."""

    numeric_periods = [
        str(period)
        for period, value in values_by_period.items()
        if _number(value) is not None
    ]
    return {
        "end_period": max(numeric_periods) if numeric_periods else None,
        "reason": "latest_available" if numeric_periods else "insufficient_history",
    }


def _periods_per_year(source: str | None, periods: list[str]) -> int:
    source_key = str(source or "").strip().lower()
    if source_key in {"ubist"}:
        return 12
    if source_key in {"iqvia", "iqvia_nsa"}:
        return 4
    return 4 if periods and all("-Q" in period for period in periods) else 12


def _elapsed_periods(start: str, end: str, periods_per_year: int) -> int:
    start_year, start_suffix = start.split("-", 1)
    end_year, end_suffix = end.split("-", 1)
    if periods_per_year == 4:
        start_index = int(start_year) * 4 + int(start_suffix.removeprefix("Q")) - 1
        end_index = int(end_year) * 4 + int(end_suffix.removeprefix("Q")) - 1
    else:
        start_index = int(start_year) * 12 + int(start_suffix) - 1
        end_index = int(end_year) * 12 + int(end_suffix) - 1
    return end_index - start_index


def _five_year_prior_period(period: str) -> str:
    year, suffix = period.split("-", 1)
    return f"{int(year) - 5}-{suffix}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _null_growth_fields(point: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``point`` with all growth metrics forced to ``None``."""

    updated = dict(point)
    updated["mom_growth_pct"] = None
    for key in ("cmgr", "cqgr"):
        if key in updated:
            updated[key] = None
    return updated


def _first_point_index(points: list[Any]) -> int | None:
    """Return the index of the earliest-period point, or 0 when unlabeled."""

    labeled = [
        (str(point["period"]), index)
        for index, point in enumerate(points)
        if isinstance(point, Mapping) and point.get("period") is not None
    ]
    if labeled:
        return min(labeled)[1]
    return 0 if points else None


def null_first_growth_point(series: Any) -> Any:
    """Force the first returned market-growth point to a non-computed baseline.

    The earliest period in a returned ``market_size_series`` cannot carry a
    valid compounded growth (it *is* the range baseline), yet cached list
    payloads and precomputed dict payloads can surface a computed value there.
    This is the single canonical enforcement point shared by every return
    boundary (dynamic ``cause_time``, ``legacy_shape`` scoped recompute,
    ``build_cache_cause`` cache emission, and the ``cache_to_response``
    composer) so the contract holds regardless of how the series was produced.

    ``mom_growth_pct``/``cmgr``/``cqgr`` on the first point become ``None``;
    the underlying value and every later point are left untouched. Accepts a
    list of period-point dicts or a ``period -> point`` mapping and returns the
    same container kind (a shallow copy).
    """

    if isinstance(series, Mapping):
        if not series:
            return series
        first_period = min(series, key=lambda period: str(period))
        first_point = series[first_period]
        if not isinstance(first_point, Mapping):
            return series
        updated = dict(series)
        updated[first_period] = _null_growth_fields(dict(first_point))
        return updated
    if isinstance(series, list):
        if not series:
            return series
        points = [dict(item) if isinstance(item, Mapping) else item for item in series]
        first_index = _first_point_index(points)
        if first_index is not None and isinstance(points[first_index], dict):
            points[first_index] = _null_growth_fields(points[first_index])
        return points
    return series
