"""Contract: cause ``data.kpi`` exposes target_brand_sales + exclusive CAGR keys.

Response-key spec (2026-07-21): the general/strategic cause KPI must carry
``target_brand_sales`` (selected brand's latest value) and report market CAGR
in mutually-exclusive ``market_cagr_5y_pct`` / ``market_cagr_3y_pct`` slots.
"""
from __future__ import annotations

import pytest

from pipeline.scripts.api.dynamic_market import cause_payload
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition


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
