from __future__ import annotations

from pathlib import Path
import math
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload


def test_recompute_uses_archive_complete_year_hhi_and_annual_ranking() -> None:
    # Given: UBIST monthly facts with one complete year and one partial year.
    focus_history = {**{f"2025-{month:02d}": 10.0 for month in range(1, 13)}, "2026-01": 100.0}
    other_history = {**{f"2025-{month:02d}": 30.0 for month in range(1, 13)}, "2026-01": 0.0}
    facts = (
        _fact("Focus", "JW", "ubist", focus_history),
        _fact("Other", "Other Co", "ubist", other_history),
    )

    # When: strategy recompute builds derived indicators.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="UBIST", measure="sales")

    # Then: HHI follows archive annual complete-year convention, while ranking
    # keeps archive full-year annual totals.
    assert payload["data"]["hhi_series_5y"] == [
        {"period": "2025", "period_full": "2025", "year": 2025, "hhi": pytest.approx(6250.0)}
    ]
    ranking = payload["data"]["brand_ranking_stacked"]
    assert ranking["years"] == [2025, 2026]
    assert ranking["rankings_by_year"]["2025"][0]["brand_key"] == "Other"
    assert ranking["rankings_by_year"]["2025"][0]["raw_value"] == pytest.approx(360.0)
    assert ranking["rankings_by_year"]["2025"][0]["ms"] == pytest.approx(75.0)
    assert ranking["rankings_by_year"]["2026"][0]["brand_key"] == "Focus"
    assert ranking["rankings_by_year"]["2026"][0]["raw_value"] == pytest.approx(100.0)
    assert payload["data"]["company_ranking_stacked"]["rankings_by_year"]["2025"][0]["company"] == "Other Co"


def test_recompute_uses_archive_endpoint_cagr_fallback_for_ei_matrix() -> None:
    # Given: IQVIA quarterly facts where the focus brand has no valid 5-year
    # endpoint but does have a valid 3-year endpoint.
    focus_history = {
        "2020-Q4": 0.0,
        "2022-Q4": 100.0,
        "2025-Q4": 133.1,
    }
    other_history = {
        "2020-Q4": 100.0,
        "2022-Q4": 100.0,
        "2025-Q4": 100.0,
    }
    facts = (
        _fact("Focus", "JW", "iqvia_nsa", focus_history),
        _fact("Other", "Other Co", "iqvia_nsa", other_history),
    )
    expected_brand_cagr = _cagr_pct(100.0, 133.1, 3)
    expected_market_cagr = _cagr_pct(200.0, 233.1, 3)

    # When: recompute builds CAGR and EI fields.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="IQVIA", measure="sales")

    # Then: archive endpoint fallback metadata is visible in the matrix payload.
    target = payload["data"]["ei_ms_matrix"]["data"][0]
    assert target["brand_key"] == "Focus"
    assert target["ei_basis"] == "endpoint_3y"
    assert target["ei_period_years"] == 3
    assert target["brand_cagr_pct"] == pytest.approx(expected_brand_cagr)
    assert target["market_cagr_pct"] == pytest.approx(expected_market_cagr)
    assert target["ei_5y"] == pytest.approx(round((expected_brand_cagr / expected_market_cagr) * 100.0, 4))
    assert payload["summary"]["cagr_5y"] == pytest.approx(expected_brand_cagr)
    assert payload["summary"]["market_cagr_5y"] == pytest.approx(expected_market_cagr)


def test_recompute_uses_archive_growth_windows_without_changing_raw_series() -> None:
    # Given: 60 monthly periods, which is the archive UBIST growth window.
    periods = _monthly_periods(start_year=2021, start_month=2, count=60)
    focus_history = {period: float(index + 1) for index, period in enumerate(periods)}
    other_history = {period: 100.0 for period in periods}
    facts = (
        _fact("Focus", "JW", "ubist", focus_history),
        _fact("Other", "Other Co", "ubist", other_history),
    )

    # When: recompute builds a cause-compatible payload.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="UBIST", measure="sales")

    # Then: raw period market series is unchanged, but growth contribution uses
    # archive first-to-last windows rather than latest-vs-previous only.
    assert payload["data"]["market_size_series"][periods[0]] == pytest.approx(101.0)
    assert payload["data"]["market_size_series"][periods[-1]] == pytest.approx(160.0)
    growth = payload["data"]["growth_contribution"]
    assert growth["period_start"] == periods[0]
    assert growth["period_end"] == periods[-1]
    assert growth["market_growth"] == pytest.approx(59.0)
    assert growth["by_brand"]["top_contributors"][0]["brand_key"] == "Focus"
    assert growth["by_brand"]["top_contributors"][0]["contribution_pct"] == pytest.approx(100.0)
    assert growth["windows"]["1y"]["period_start"] == periods[-12]
    assert growth["windows"]["1y"]["period_end"] == periods[-1]


def _fact(brand_key: str, company: str, source: str, raw_value_history: dict[str, float]) -> StrategyFact:
    """Build one strategy recompute fact for archive parity tests."""

    return StrategyFact(
        market_id="strategy_001",
        raw_fact_id=f"raw:{brand_key}",
        brand_key=brand_key,
        brand_name=brand_key,
        company=company,
        source=source,
        measure="sales",
        unit_label="KRW",
        raw_value_history=raw_value_history,
    )


def _cagr_pct(start_value: float, end_value: float, years: int) -> float:
    """Return archive-style endpoint CAGR rounded like EI metadata."""

    return round((math.pow(end_value / start_value, 1 / years) - 1) * 100.0, 4)


def _monthly_periods(*, start_year: int, start_month: int, count: int) -> tuple[str, ...]:
    """Return chronological ``YYYY-MM`` periods for synthetic UBIST facts."""

    periods: list[str] = []
    year = start_year
    month = start_month
    for _ in range(count):
        periods.append(f"{year}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(periods)
