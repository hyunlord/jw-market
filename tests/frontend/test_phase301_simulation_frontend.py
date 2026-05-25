from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


FRONTEND_HTML = Path("docs/reference/jw_market_hardcoded_mockup_v3_4.html")
FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def test_phase301_frontend_no_anomaly_or_stress_ui() -> None:
    html = FRONTEND_HTML.read_text(encoding="utf-8")

    assert "simulation-anomaly-box" not in html
    assert "renderSimulationAnomaliesFromData" not in html
    assert ".stress" not in html
    assert "stress." not in html


def test_phase301_simulation_chart_horizon_follows_kpi_toggle() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{FRONTEND_URL}?phase301_nocache=1", wait_until="networkidle", timeout=60000)

        page.select_option("#brand-select", value="가드메트")
        page.click('.tab[data-page="deep"]')
        page.wait_for_function("() => window.currentDeepData && window.currentDeepData.brand === '가드메트'", timeout=60000)
        page.evaluate("() => switchDeepTab('simulation')")
        page.wait_for_selector("#page-deep #tab-simulation .sim-prediction-grid", state="visible", timeout=30000)

        page.click('#sim-horizon .toggle-btn[data-years="1"]')
        page.wait_for_timeout(300)
        chart_text_1y = page.locator("#page-deep #tab-simulation .chart-svg").text_content()
        assert "2027-04" in chart_text_1y

        page.click('#sim-horizon .toggle-btn[data-years="3"]')
        page.wait_for_timeout(300)
        chart_text_3y = page.locator("#page-deep #tab-simulation .chart-svg").text_content()
        assert "2029-04" in chart_text_3y

        browser.close()
