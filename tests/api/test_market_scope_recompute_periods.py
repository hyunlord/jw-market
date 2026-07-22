from __future__ import annotations

from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload


def test_recompute_requires_archive_endpoint_for_monthly_cagr() -> None:
    # Given: UBIST-style monthly strategy facts with a one-year span.
    facts = (
        _ubist_fact("Focus", "JW", {"2025-01": 100.0, "2026-01": 200.0}),
        _ubist_fact("Other", "Other Co", {"2025-01": 100.0, "2026-01": 100.0}),
    )

    # When: strategy recompute annualizes the history.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="UBIST", measure="sales")

    # Then: the archive endpoint policy does not invent a 1-year CAGR when
    # exact 5-year and 3-year endpoints are absent.
    assert payload["data"]["kpi"]["brand_cagr_pct"] is None
    assert payload["data"]["kpi"]["market_cagr_5y_pct"] is None
    assert _market_size_value(payload, "2026-01") == 300.0
    assert payload["data"]["brand_ranking"]["rankings_by_year"]["2026"][0]["brand_key"] == "Focus"


def test_recompute_handles_iqvia_quarterly_cagr_when_periods_are_yyyy_qn() -> None:
    # Given: IQVIA-style quarterly strategy facts with a five-year span.
    facts = (
        _iqvia_fact("Focus", "JW", {"2020-Q4": 100.0, "2025-Q4": 200.0}),
        _iqvia_fact("Other", "Other Co", {"2020-Q4": 300.0, "2025-Q4": 300.0}),
    )

    # When: strategy recompute annualizes quarterly periods.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="IQVIA", measure="sales")

    # Then: no YYYY-Qn parsing exception occurs and quarterly endpoint CAGR
    # follows archive rounding.
    assert payload["data"]["kpi"]["brand_cagr_pct"] == pytest.approx(14.8698)
    assert payload["data"]["kpi"]["market_cagr_5y_pct"] == pytest.approx(4.56)
    assert payload["data"]["kpi"]["target_share_pct"] == pytest.approx(40.0)
    assert _market_size_value(payload, "2025-Q4") == 500.0
    assert payload["data"]["brand_ranking"]["rankings_by_year"]["2025"][1]["brand_key"] == "Focus"
    assert payload["data"]["hhi_series_5y"] == []


def test_recompute_uses_19_quarter_market_cagr_in_the_five_year_slot() -> None:
    # Given: an IQVIA market whose first available quarter is 19 quarters before latest.
    periods = _quarter_labels("2021-Q2", "2026-Q1")
    focus = {period: 100.0 + index for index, period in enumerate(periods)}
    other = {period: 300.0 + (index * 2.0) for index, period in enumerate(periods)}
    facts = (
        _iqvia_fact("Focus", "JW", focus),
        _iqvia_fact("Other", "Other Co", other),
    )
    latest_market = focus[periods[-1]] + other[periods[-1]]
    first_market = focus[periods[0]] + other[periods[0]]
    expected = ((latest_market / first_market) ** (1 / 4.75) - 1) * 100

    # When: market-scope recompute builds the KPI payload.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="IQVIA", measure="sales")

    # Then: it uses the same 19-quarter substitute and exclusive slots as dynamic cause.
    assert payload["data"]["kpi"]["market_cagr_5y_pct"] == pytest.approx(round(expected, 2))
    assert payload["data"]["kpi"]["market_cagr_3y_pct"] is None


def _ubist_fact(brand_key: str, company: str, raw_value_history: dict[str, float]) -> StrategyFact:
    """Build a monthly UBIST recompute fact."""

    return StrategyFact(
        market_id="strategy_001",
        raw_fact_id=f"raw:{brand_key}",
        brand_key=brand_key,
        brand_name=brand_key,
        company=company,
        source="ubist",
        measure="sales",
        unit_label="KRW",
        raw_value_history=raw_value_history,
    )


def _iqvia_fact(brand_key: str, company: str, raw_value_history: dict[str, float]) -> StrategyFact:
    """Build a quarterly IQVIA recompute fact."""

    return StrategyFact(
        market_id="strategy_001",
        raw_fact_id=f"raw:{brand_key}",
        brand_key=brand_key,
        brand_name=brand_key,
        company=company,
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        raw_value_history=raw_value_history,
    )


def _market_size_value(payload: dict[str, object], period: str) -> float:
    """Return one FE-facing market-size point value from a recompute payload."""

    data = payload["data"]
    assert isinstance(data, dict)
    series = data["market_size_series"]
    assert isinstance(series, list)
    values = {
        str(point["period"]): float(point["value"])
        for point in series
        if isinstance(point, dict)
    }
    return values[period]


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
