from __future__ import annotations

from pathlib import Path


FRONTEND_HTML = Path("docs/reference/jw_market_hardcoded_mockup_v3_4.html")


def test_phase304_frontend_uses_forecast_anchor_without_duplicate_x_gap() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")

    assert "[Phase 30.4] anchor mismatch" in html
    assert "const hasForecastAnchor =" in html
    assert "forecastPeriods[0] === historyPeriods[historyPeriods.length - 1]" in html
    assert "const total = Math.max(historyPeriods.length + forecastPeriods.length - (hasForecastAnchor ? 1 : 0), 2);" in html
    assert "const offset = hasForecastAnchor ? Math.max(historyValues.length - 1, 0) : historyValues.length;" in html
    assert "const periods = hasForecastAnchor ? [...historyPeriods, ...forecastPeriods.slice(1)] : [...historyPeriods, ...forecastPeriods];" in html


def test_phase304_kpi_horizon_index_accounts_for_anchor_point() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")

    assert "const horizonIdx = horizonYears * stepsPerYear;" in html
    assert "const horizonIdx = horizonYears * stepsPerYear - 1;" not in html
