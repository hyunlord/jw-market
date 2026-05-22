"""Phase 7 frontend v3.2 smoke tests."""

from __future__ import annotations

from playwright.sync_api import sync_playwright


FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_2.html"


def test_v3_2_loads_status_cards_and_spec_charts() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle", timeout=30000)

        assert page.locator(".brand-card").count() == 25

        page.click("[data-page-target='cause']")
        page.select_option("#cause-brand-select", "리바로")
        page.click("#cause-load-button")
        page.wait_for_timeout(2500)

        canvases = page.locator("canvas[id^='chart-']")
        assert canvases.count() >= 11
        assert page.locator("text=Brand Ranking Trend").count() == 1
        assert page.locator("text=Growth / MS Matrix").count() == 1
        assert page.locator("text=Analysis Levels").count() == 1
        assert page.locator("text=No matrix rows in response").count() == 0

        browser.close()


def test_v3_2_deep_analysis_history_only_copy_renders() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle", timeout=30000)
        page.select_option("#brand-select", "리바로")
        page.click("[data-page-target='deep']")
        page.click("#deep-load-button")
        page.wait_for_timeout(1500)

        assert page.locator("text=Forecast combos").count() == 1
        assert page.locator("text=forecast pending").count() > 0
        assert page.locator("text=AI analysis").count() == 1

        browser.close()
