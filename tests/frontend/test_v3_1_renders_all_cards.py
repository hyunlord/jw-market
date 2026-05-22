from __future__ import annotations

from playwright.sync_api import sync_playwright


FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_1.html"


def test_v3_1_loads_25_brand_cards():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.goto(FRONTEND_URL, wait_until="networkidle", timeout=20_000)
        assert len(page.query_selector_all(".brand-card")) == 25
        browser.close()


def test_v3_1_renders_all_cause_chart_cards():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.goto(FRONTEND_URL, wait_until="networkidle", timeout=20_000)
        page.locator('button[data-action="cause"]').first.click()
        page.wait_for_timeout(3_000)

        assert len(page.query_selector_all(".chart-card")) >= 12
        assert len(page.query_selector_all('canvas[id^="chart-"]')) >= 12
        assert page.locator("text=No matrix rows in response").count() == 0
        browser.close()


def test_v3_1_chart_canvases_have_drawn_content():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.goto(FRONTEND_URL, wait_until="networkidle", timeout=20_000)
        page.locator('button[data-action="cause"]').first.click()
        page.wait_for_timeout(3_000)

        canvases = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('canvas[id^="chart-"]')).map((canvas) => ({
              id: canvas.id,
              width: canvas.width,
              height: canvas.height,
              blank: canvas.toDataURL().length < 2000
            }))
            """
        )
        active = [canvas for canvas in canvases if canvas["width"] > 0 and canvas["height"] > 0 and not canvas["blank"]]
        assert len(active) >= 12, canvases
        browser.close()
