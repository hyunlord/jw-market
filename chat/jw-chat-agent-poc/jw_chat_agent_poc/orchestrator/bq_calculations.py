from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Final, TypeAlias


ONE: Final = Decimal("1")
ZERO: Final = Decimal("0")
DecimalValue: TypeAlias = Decimal | None


@dataclass(frozen=True, slots=True)
class GrowthDecomposition:
    brand_growth: DecimalValue
    market_growth: DecimalValue
    excess_growth: DecimalValue


@dataclass(frozen=True, slots=True)
class DeltaResult:
    delta: DecimalValue
    delta_rate: DecimalValue


@dataclass(frozen=True, slots=True)
class AccelerationResult:
    delta: DecimalValue
    acceleration_rate: DecimalValue


@dataclass(frozen=True, slots=True)
class OperandDelta:
    left: DecimalValue
    right: DecimalValue
    absolute_delta: DecimalValue
    relative_delta: DecimalValue


@dataclass(frozen=True, slots=True)
class ChangeOperands:
    before: DecimalValue
    after: DecimalValue


@dataclass(frozen=True, slots=True)
class AlignedChange:
    activity_delta: DecimalValue
    performance_delta: DecimalValue
    alignment_product: DecimalValue


@dataclass(frozen=True, slots=True)
class AlignedChanges:
    activity_delta: DecimalValue
    activity_change_rate: DecimalValue
    performance_delta: DecimalValue
    performance_change_rate: DecimalValue


def calculate_cagr(start: DecimalValue, end: DecimalValue, *, periods: Decimal) -> DecimalValue:
    if start is None or end is None or start <= ZERO or end < ZERO or periods <= ZERO:
        return None
    try:
        with localcontext() as context:
            context.prec = 28
            return (end / start) ** (ONE / periods) - ONE
    except InvalidOperation:
        return None


def conditional_trend_forecast(
    *,
    baseline: DecimalValue,
    trend_rate: DecimalValue,
    threshold: Decimal,
    periods: Decimal,
) -> DecimalValue:
    if baseline is None or trend_rate is None or periods < ZERO:
        return None
    if abs(trend_rate) < threshold:
        return baseline
    return baseline * (ONE + trend_rate) ** periods


def share_of_growth(*, brand_growth: DecimalValue, market_growth: DecimalValue) -> DecimalValue:
    if brand_growth is None or market_growth is None or market_growth == 0:
        return None
    return brand_growth / market_growth


def market_vs_brand_growth_decomposition(
    *,
    brand_growth: DecimalValue,
    market_growth: DecimalValue,
) -> GrowthDecomposition:
    excess_growth = None
    if brand_growth is not None and market_growth is not None:
        excess_growth = brand_growth - market_growth
    return GrowthDecomposition(
        brand_growth=brand_growth,
        market_growth=market_growth,
        excess_growth=excess_growth,
    )


def gain_loss_delta(*, current: DecimalValue, previous: DecimalValue) -> DeltaResult:
    delta, rate = _delta_and_rate(current=current, baseline=previous)
    return DeltaResult(delta=delta, delta_rate=rate)


def cohort_z_score(*, value: DecimalValue, mean: DecimalValue, stddev: DecimalValue) -> DecimalValue:
    if value is None or mean is None or stddev is None or stddev == 0:
        return None
    return (value - mean) / stddev


def launch_acceleration(
    *,
    current_velocity: DecimalValue,
    prior_velocity: DecimalValue,
) -> AccelerationResult:
    delta, rate = _delta_and_rate(current=current_velocity, baseline=prior_velocity)
    return AccelerationResult(delta=delta, acceleration_rate=rate)


def source_divergence(*, primary: DecimalValue, comparison: DecimalValue) -> OperandDelta:
    absolute_delta, relative_delta = _delta_and_rate(current=primary, baseline=comparison)
    return OperandDelta(left=primary, right=comparison, absolute_delta=absolute_delta, relative_delta=relative_delta)


def source_lag(*, current: DecimalValue, lagged: DecimalValue) -> OperandDelta:
    absolute_delta, relative_delta = _delta_and_rate(current=current, baseline=lagged)
    return OperandDelta(left=current, right=lagged, absolute_delta=absolute_delta, relative_delta=relative_delta)


def patient_sales_ratio(*, sales: DecimalValue, patients: DecimalValue) -> DecimalValue:
    if sales is None or patients is None or patients == 0:
        return None
    return sales / patients


def aligned_activity_performance_change(
    *,
    activity_delta: DecimalValue,
    performance_delta: DecimalValue,
) -> AlignedChange:
    product = None
    if activity_delta is not None and performance_delta is not None:
        product = activity_delta * performance_delta
    return AlignedChange(
        activity_delta=activity_delta,
        performance_delta=performance_delta,
        alignment_product=product,
    )


def aligned_activity_performance_changes(
    *,
    activity: ChangeOperands,
    performance: ChangeOperands,
) -> AlignedChanges:
    activity_delta, activity_rate = _delta_and_rate(current=activity.after, baseline=activity.before)
    performance_delta, performance_rate = _delta_and_rate(current=performance.after, baseline=performance.before)
    return AlignedChanges(
        activity_delta=activity_delta,
        activity_change_rate=activity_rate,
        performance_delta=performance_delta,
        performance_change_rate=performance_rate,
    )


def _delta_and_rate(
    *,
    current: DecimalValue,
    baseline: DecimalValue,
) -> tuple[DecimalValue, DecimalValue]:
    if current is None or baseline is None:
        return None, None
    delta = current - baseline
    if baseline == ZERO:
        return delta, None
    return delta, delta / baseline
