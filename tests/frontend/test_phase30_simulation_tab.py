from __future__ import annotations

from playwright.sync_api import sync_playwright


FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def test_phase30_simulation_tab_renders_backend_payload() -> None:
    """Simulation tab must render Phase 30 backend scenarios and confidence."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{FRONTEND_URL}?phase30_nocache=1", wait_until="networkidle", timeout=60000)

        page.select_option("#brand-select", value="가드메트")
        page.click('.tab[data-page="deep"]')
        page.wait_for_function("() => window.currentDeepData && window.currentDeepData.brand === '가드메트'", timeout=60000)
        page.evaluate("() => switchDeepTab('simulation')")
        page.wait_for_function(
            """() => {
              const sim = window.currentDeepData?.data?.simulation?.by_combo?.['UBIST.sales'];
              return !!sim?.phase30_baseline && !!sim.by_brand?.['가드메트'];
            }""",
            timeout=60000,
        )
        page.wait_for_selector("#page-deep #tab-simulation .sim-prediction-grid", state="visible", timeout=30000)

        assert page.locator("#simulation-empty-message").is_hidden()
        assert page.locator("#page-deep #tab-simulation .sim-card.confidence .sim-value").inner_text().strip() != "—"
        assert "v0.9.1 data-size dispatch" in page.locator("#page-deep .note-banner").inner_text()
        assert page.locator("#simulation-anomaly-box").count() == 0
        assert page.locator("#page-deep #tab-simulation .chart-svg polyline").count() >= 3

        browser.close()
