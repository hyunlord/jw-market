"""Contract: cause ``data.kpi`` exposes target_brand_sales + exclusive CAGR keys.

Response-key spec (2026-07-21): the general/strategic cause KPI must carry
``target_brand_sales`` (selected brand's latest value) and report market CAGR
in mutually-exclusive ``market_cagr_5y_pct`` / ``market_cagr_3y_pct`` slots.
"""
from __future__ import annotations

import pytest

from pipeline.scripts.api.dynamic_market import cause_payload
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition
from pipeline.scripts.etl.cache_build_common import calculate_ei_with_fallback, market_cagr_exclusive


def _metrics(start_period: str, latest: str, market_start: float, market_latest: float) -> tuple[AggregatedMetrics, BrandMetric]:
    focus = BrandMetric(
        "focus", "Focus", "C10A1", 10.0, 0.0, 1, latest, 10.0,
        monthly_series=({"period": start_period, "value": 5.0}, {"period": latest, "value": 10.0}),
    )
    other = BrandMetric(
        "other", "Other", "C10A1", 20.0, 0.0, 2, latest, 20.0,
        monthly_series=({"period": start_period, "value": 15.0}, {"period": latest, "value": 20.0}),
    )
    metrics = AggregatedMetrics(
        source="ubist", measure="sales", unit_label="KRW", market_size=0.0, hhi=None, cagr=None,
        monthly_series=(
            {"period": start_period, "market_size": market_start},
            {"period": latest, "market_size": market_latest},
        ),
        brands=(other, focus),
        all_brands=(other, focus),
    )
    return metrics, focus


def _kpi(start_period: str) -> dict:
    metrics, focus = _metrics(start_period, "2026-05", 100.0, 200.0)
    data = cause_payload.build_cause_data(
        definition=MarketDefinition(view="general", filter_echo={}, source="ubist", measure="sales"),
        metrics=metrics,
        focus=focus,
    )
    return data["kpi"]


def test_target_brand_sales_matches_selected_recent_value() -> None:
    kpi = _kpi("2025-05")
    assert "target_brand_sales" in kpi
    assert kpi["target_brand_sales"] == kpi["brand_value_recent"] == 10.0


def test_market_cagr_3y_key_present() -> None:
    kpi = _kpi("2025-05")
    assert "market_cagr_3y_pct" in kpi
    assert "market_cagr_5y_pct" in kpi


@pytest.mark.parametrize(
    ("start_period", "expect_5y", "expect_3y"),
    [
        ("2021-05", True, False),   # 5y endpoint present -> 5y slot only
        ("2023-05", False, True),   # only 3y endpoint -> 3y slot only
        ("2025-05", False, False),  # neither -> both null
    ],
)
def test_cagr_slots_are_exclusive(start_period: str, expect_5y: bool, expect_3y: bool) -> None:
    kpi = _kpi(start_period)
    has_5y = kpi["market_cagr_5y_pct"] is not None
    has_3y = kpi["market_cagr_3y_pct"] is not None
    assert has_5y is expect_5y
    assert has_3y is expect_3y
    # ★ failure injection: silent 5y->3y fallback would light both slots.
    assert not (has_5y and has_3y)


@pytest.mark.parametrize(
    ("start_period", "expect_5y", "expect_3y"),
    [
        ("2021-05", True, False),
        ("2023-05", False, True),
        ("2025-05", False, False),
    ],
)
def test_general_cause_brand_cagr_slots_are_present_and_exclusive(
    start_period: str,
    expect_5y: bool,
    expect_3y: bool,
) -> None:
    kpi = _kpi(start_period)

    has_5y = kpi["brand_cagr_5y_pct"] is not None
    has_3y = kpi["brand_cagr_3y_pct"] is not None
    assert has_5y is expect_5y
    assert has_3y is expect_3y
    assert not (has_5y and has_3y)


def test_iqvia_uses_only_the_19_quarter_substitute_for_five_year_cagr() -> None:
    # Given: quarterly endpoints at 20, 19, and 18 quarters before 2026-Q1.
    exact_20q = _quarterly_series("2021-Q1", "2026-Q1", 100.0, 200.0)
    substitute_19q = _quarterly_series("2021-Q2", "2026-Q1", 100.0, 200.0)
    too_short_18q = _quarterly_series("2021-Q3", "2026-Q1", 100.0, 200.0)

    # When: the exclusive serving CAGR policy selects its endpoint.
    exact = market_cagr_exclusive(exact_20q)
    substitute = market_cagr_exclusive(substitute_19q)
    too_short = market_cagr_exclusive(too_short_18q)

    # Then: only the single-quarter shortfall enters the 5y slot.
    assert exact == (pytest.approx(_cagr_pct(100.0, 200.0, 5.0), abs=0.01), None)
    assert substitute == (pytest.approx(_cagr_pct(100.0, 200.0, 4.75), abs=0.01), None)
    assert too_short[0] is None
    assert too_short[1] is not None


def test_iqvia_19_quarter_ei_uses_the_same_actual_elapsed_years() -> None:
    # Given: both brand and market have exactly the permitted 19-quarter span.
    brand = _quarterly_series("2021-Q2", "2026-Q1", 100.0, 200.0)
    market = _quarterly_series("2021-Q2", "2026-Q1", 400.0, 600.0)

    # When: EI selects its shared market and brand CAGR basis.
    result = calculate_ei_with_fallback(brand, market)

    # Then: EI remains on the 5y basis but records the actual 4.75-year exponent.
    assert result["basis"] == "endpoint_5y"
    assert result["period_years"] == 4.75
    assert result["brand_cagr_pct"] == pytest.approx(_cagr_pct_precise(100.0, 200.0, 4.75))
    assert result["market_cagr_pct"] == pytest.approx(_cagr_pct_precise(400.0, 600.0, 4.75))


def test_ei_does_not_mix_19_and_20_quarter_cagr_windows() -> None:
    # Given: the brand starts one quarter later than the containing market.
    brand = _quarterly_series("2021-Q2", "2026-Q1", 100.0, 200.0)
    market = _quarterly_series("2021-Q1", "2026-Q1", 400.0, 600.0)

    # When: EI selects a common comparison window.
    result = calculate_ei_with_fallback(brand, market)

    # Then: it rejects the mixed 4.75y/5y pair and uses the common 3y window.
    assert result["basis"] == "endpoint_3y"
    assert result["period_years"] == 3
    assert result["brand_start_period"] == "2023-Q1"
    assert result["market_start_period"] == "2023-Q1"


def test_market_meta_uses_the_same_exclusive_cagr_as_data_kpi() -> None:
    # Given: an IQVIA runtime payload with a 19-quarter market history.
    periods = _quarter_labels("2021-Q2", "2026-Q1")
    focus_series = tuple({"period": period, "value": 100.0 + index} for index, period in enumerate(periods))
    focus = BrandMetric(
        "focus",
        "Focus",
        "A10N1",
        10.0,
        0.0,
        1,
        periods[-1],
        10.0,
        monthly_series=focus_series,
    )
    metrics = AggregatedMetrics(
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        market_size=200.0,
        hhi=None,
        cagr=-999.0,
        monthly_series=tuple(
            {"period": period, "market_size": 200.0 + (index * 5.0)}
            for index, period in enumerate(periods)
        ),
        brands=(focus,),
        all_brands=(focus,),
    )

    # When: the cause-compatible response is assembled.
    payload = cause_payload.build_cause_payload(
        definition=MarketDefinition(
            view="strategic_ml",
            filter_echo={},
            source="iqvia_nsa",
            measure="sales",
            focus_brand_key="focus",
        ),
        metrics=metrics,
    )

    # Then: response branches cannot publish different values for the same KPI.
    assert payload["market_meta"]["market_cagr_5y_pct"] == payload["data"]["kpi"]["market_cagr_5y_pct"]


def _quarterly_series(start: str, end: str, start_value: float, end_value: float) -> dict[str, float]:
    periods = _quarter_labels(start, end)
    step = (end_value - start_value) / (len(periods) - 1)
    return {period: start_value + (index * step) for index, period in enumerate(periods)}


def _quarter_labels(start: str, end: str) -> tuple[str, ...]:
    year, quarter = (int(item) for item in start.replace("-Q", "-").split("-"))
    end_year, end_quarter = (int(item) for item in end.replace("-Q", "-").split("-"))
    result: list[str] = []
    while (year, quarter) <= (end_year, end_quarter):
        result.append(f"{year}-Q{quarter}")
        quarter += 1
        if quarter == 5:
            year += 1
            quarter = 1
    return tuple(result)


def _cagr_pct(start_value: float, end_value: float, years: float) -> float:
    return round(((end_value / start_value) ** (1 / years) - 1) * 100, 2)


def _cagr_pct_precise(start_value: float, end_value: float, years: float) -> float:
    return round(((end_value / start_value) ** (1 / years) - 1) * 100, 4)
