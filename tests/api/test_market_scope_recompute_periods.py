from __future__ import annotations

from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload


def test_recompute_preserves_monthly_cagr_when_periods_are_yyyy_mm() -> None:
    # Given: UBIST-style monthly strategy facts with a one-year span.
    facts = (
        _ubist_fact("Focus", "JW", {"2025-01": 100.0, "2026-01": 200.0}),
        _ubist_fact("Other", "Other Co", {"2025-01": 100.0, "2026-01": 100.0}),
    )

    # When: strategy recompute annualizes the history.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="UBIST", measure="sales")

    # Then: the existing monthly CAGR behavior and latest ranking stay intact.
    assert payload["summary"]["cagr_5y"] == pytest.approx(100.0)
    assert payload["summary"]["market_cagr_5y"] == pytest.approx(50.0)
    assert payload["data"]["market_size_series"]["2026-01"] == 300.0
    assert payload["data"]["brand_ranking"]["2026-01"][0]["brand_key"] == "Focus"


def test_recompute_handles_iqvia_quarterly_cagr_when_periods_are_yyyy_qn() -> None:
    # Given: IQVIA-style quarterly strategy facts with a five-year span.
    facts = (
        _iqvia_fact("Focus", "JW", {"2020-Q4": 100.0, "2025-Q4": 200.0}),
        _iqvia_fact("Other", "Other Co", {"2020-Q4": 300.0, "2025-Q4": 300.0}),
    )

    # When: strategy recompute annualizes quarterly periods.
    payload = recompute_strategy_payload(facts, focus_brand_key="Focus", source="IQVIA", measure="sales")

    # Then: no YYYY-Qn parsing exception occurs and quarterly CAGR uses quarters / 4.
    assert payload["summary"]["cagr_5y"] == pytest.approx(14.869835)
    assert payload["summary"]["market_cagr_5y"] == pytest.approx(4.563955)
    assert payload["summary"]["market_share"] == pytest.approx(40.0)
    assert payload["data"]["market_size_series"]["2025-Q4"] == 500.0
    assert payload["data"]["brand_ranking"]["2025-Q4"][1]["brand_key"] == "Focus"
    assert payload["data"]["hhi_series_5y"]["2025-Q4"] == pytest.approx(5200.0)


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
