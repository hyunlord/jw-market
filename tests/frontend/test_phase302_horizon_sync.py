from __future__ import annotations

from pathlib import Path


FRONTEND_HTML = Path("docs/reference/jw_market_hardcoded_mockup_v3_4.html")


def test_phase302_simulation_horizon_handler_rerenders_chart_path() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")

    assert "const forecastSteps = Math.min(horizonIdx + 1, (simBrand.forecast_periods || []).length);" in html
    assert "const historySteps = Math.min(Math.max(forecastSteps, stepsPerYear)" in html
    assert "_deepState.simulation.horizonYears = y;" in html
    assert "if (_deepState.data) renderSimulationCardsFromData(_deepState.data);" in html
    assert "simulation 카드 + 차트 동시 갱신" in html
