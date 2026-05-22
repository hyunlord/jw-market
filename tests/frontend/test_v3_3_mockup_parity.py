from __future__ import annotations

from playwright.sync_api import sync_playwright


URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_3.html"


def test_v3_3_status_page_has_five_kpis_and_25_brands() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL)
        page.wait_for_load_state("networkidle", timeout=15000)

        assert page.locator(".kpi-card").count() == 5
        assert page.locator(".brand-card").count() == 25

        for label in ("총 매출", "평균 M/S", "매출 상승", "매출 하락", "CAGR 5y"):
            assert page.locator(f"text={label}").count() >= 1

        browser.close()


def test_v3_3_cause_page_renders_original_card_set() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL)
        page.wait_for_load_state("networkidle", timeout=15000)

        page.click('button[data-page-target="cause"]')
        page.wait_for_timeout(2500)

        for title in (
            "A.1",
            "A.2",
            "A.3",
            "A.4",
            "A.5",
            "B.1",
            "B.2",
            "C.1",
            "D.1",
            "D.2",
            "D.3",
        ):
            assert page.locator(f"text={title}").count() >= 1

        assert page.locator('canvas[id^="chart-"]').count() >= 8
        assert page.locator("text=market_landscape = 일반뷰").count() == 0

        browser.close()


def test_v3_3_deep_page_renders_forecast_events_and_ai() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL)
        page.wait_for_load_state("networkidle", timeout=15000)

        page.click('button[data-page-target="deep"]')
        page.wait_for_timeout(2500)

        for label in ("Forecast", "Simulation", "Events", "AI Analysis"):
            assert page.locator(f"text={label}").count() >= 1
        assert page.locator("text=예측 미구현").count() >= 1

        browser.close()
