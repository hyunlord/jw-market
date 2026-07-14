from __future__ import annotations

from decimal import Decimal

from jw_chat_agent_poc.orchestrator.bq_calculations import (
    ChangeOperands,
    aligned_activity_performance_change,
    aligned_activity_performance_changes,
    calculate_cagr,
    cohort_z_score,
    conditional_trend_forecast,
    gain_loss_delta,
    launch_acceleration,
    market_vs_brand_growth_decomposition,
    patient_sales_ratio,
    share_of_growth,
    source_divergence,
    source_lag,
)


def test_calculate_cagr_when_endpoint_is_missing_then_preserves_none() -> None:
    result = calculate_cagr(Decimal("100"), None, periods=Decimal("2"))

    assert result is None


def test_calculate_cagr_when_inputs_are_valid_then_returns_decimal_rate() -> None:
    result = calculate_cagr(Decimal("100"), Decimal("121"), periods=Decimal("2"))

    assert result == Decimal("0.1")


def test_conditional_trend_forecast_when_trend_is_below_threshold_then_holds_baseline() -> None:
    result = conditional_trend_forecast(
        baseline=Decimal("100"),
        trend_rate=Decimal("0.05"),
        threshold=Decimal("0.10"),
        periods=Decimal("2"),
    )

    assert result == Decimal("100")


def test_conditional_trend_forecast_when_trend_is_large_then_compounds() -> None:
    result = conditional_trend_forecast(
        baseline=Decimal("100"),
        trend_rate=Decimal("0.10"),
        threshold=Decimal("0.05"),
        periods=Decimal("2"),
    )

    assert result == Decimal("121.00")


def test_share_of_growth_when_market_growth_is_missing_then_preserves_none() -> None:
    result = share_of_growth(brand_growth=Decimal("12"), market_growth=None)

    assert result is None


def test_share_of_growth_when_growth_exists_then_returns_contribution_ratio() -> None:
    result = share_of_growth(brand_growth=Decimal("12"), market_growth=Decimal("48"))

    assert result == Decimal("0.25")


def test_market_vs_brand_growth_decomposition_when_market_is_missing_then_gap_is_none() -> None:
    result = market_vs_brand_growth_decomposition(brand_growth=Decimal("0.08"), market_growth=None)

    assert result.brand_growth == Decimal("0.08")
    assert result.market_growth is None
    assert result.excess_growth is None


def test_market_vs_brand_growth_decomposition_when_both_exist_then_returns_excess() -> None:
    result = market_vs_brand_growth_decomposition(brand_growth=Decimal("0.08"), market_growth=Decimal("0.05"))

    assert result.excess_growth == Decimal("0.03")


def test_gain_loss_delta_when_previous_is_missing_then_preserves_none() -> None:
    result = gain_loss_delta(current=Decimal("42"), previous=None)

    assert result.delta is None
    assert result.delta_rate is None


def test_gain_loss_delta_when_previous_exists_then_returns_delta_and_rate() -> None:
    result = gain_loss_delta(current=Decimal("42"), previous=Decimal("40"))

    assert result.delta == Decimal("2")
    assert result.delta_rate == Decimal("0.05")


def test_cohort_z_score_when_stddev_is_zero_then_returns_none() -> None:
    result = cohort_z_score(value=Decimal("10"), mean=Decimal("8"), stddev=Decimal("0"))

    assert result is None


def test_cohort_z_score_when_inputs_exist_then_returns_standard_score() -> None:
    result = cohort_z_score(value=Decimal("10"), mean=Decimal("8"), stddev=Decimal("4"))

    assert result == Decimal("0.5")


def test_launch_acceleration_when_prior_velocity_is_missing_then_preserves_none() -> None:
    result = launch_acceleration(current_velocity=Decimal("15"), prior_velocity=None)

    assert result.delta is None
    assert result.acceleration_rate is None


def test_launch_acceleration_when_velocities_exist_then_returns_change_and_rate() -> None:
    result = launch_acceleration(current_velocity=Decimal("15"), prior_velocity=Decimal("10"))

    assert result.delta == Decimal("5")
    assert result.acceleration_rate == Decimal("0.5")


def test_source_divergence_and_lag_when_operands_are_missing_then_preserve_none() -> None:
    divergence = source_divergence(primary=Decimal("105"), comparison=None)
    lag = source_lag(current=Decimal("105"), lagged=None)

    assert divergence.absolute_delta is None
    assert divergence.relative_delta is None
    assert lag.absolute_delta is None
    assert lag.relative_delta is None


def test_source_divergence_and_lag_when_values_exist_then_return_operands() -> None:
    divergence = source_divergence(primary=Decimal("105"), comparison=Decimal("100"))
    lag = source_lag(current=Decimal("105"), lagged=Decimal("100"))

    assert divergence.left == Decimal("105")
    assert divergence.right == Decimal("100")
    assert divergence.absolute_delta == Decimal("5")
    assert divergence.relative_delta == Decimal("0.05")
    assert lag.left == Decimal("105")
    assert lag.right == Decimal("100")
    assert lag.absolute_delta == Decimal("5")
    assert lag.relative_delta == Decimal("0.05")


def test_patient_sales_ratio_when_patients_missing_then_preserves_none() -> None:
    result = patient_sales_ratio(sales=Decimal("1000"), patients=None)

    assert result is None


def test_patient_sales_ratio_when_values_exist_then_returns_sales_per_patient() -> None:
    result = patient_sales_ratio(sales=Decimal("1000"), patients=Decimal("25"))

    assert result == Decimal("40")


def test_aligned_activity_performance_change_when_activity_missing_then_preserves_none() -> None:
    result = aligned_activity_performance_change(activity_delta=None, performance_delta=Decimal("0.08"))

    assert result.activity_delta is None
    assert result.performance_delta == Decimal("0.08")
    assert result.alignment_product is None


def test_aligned_activity_performance_change_when_values_exist_then_returns_directional_product() -> None:
    result = aligned_activity_performance_change(activity_delta=Decimal("0.25"), performance_delta=Decimal("0.08"))

    assert result.alignment_product == Decimal("0.0200")


def test_aligned_activity_performance_changes_when_operands_exist_then_returns_parallel_changes() -> None:
    result = aligned_activity_performance_changes(
        activity=ChangeOperands(before=Decimal("100"), after=Decimal("125")),
        performance=ChangeOperands(before=Decimal("40"), after=Decimal("44")),
    )

    assert result.activity_delta == Decimal("25")
    assert result.activity_change_rate == Decimal("0.25")
    assert result.performance_delta == Decimal("4")
    assert result.performance_change_rate == Decimal("0.1")


def test_aligned_activity_performance_changes_when_operands_missing_then_preserves_none() -> None:
    result = aligned_activity_performance_changes(
        activity=ChangeOperands(before=None, after=Decimal("125")),
        performance=ChangeOperands(before=Decimal("40"), after=None),
    )

    assert result.activity_delta is None
    assert result.activity_change_rate is None
    assert result.performance_delta is None
    assert result.performance_change_rate is None
