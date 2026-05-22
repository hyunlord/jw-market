from __future__ import annotations

from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright


API_BASE = "http://127.0.0.1:8013"


def test_swagger_origin_preflight_is_allowed():
    brand = quote("가드메트")
    response = requests.options(
        f"{API_BASE}/api/cause/{brand}?view=market_landscape&source=UBIST&measure=sales",
        headers={
            "Origin": API_BASE,
            "Access-Control-Request-Method": "GET",
        },
        timeout=10,
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == API_BASE


def test_swagger_docs_page_can_fetch_cause():
    brand = quote("가드메트")
    path = f"/api/cause/{brand}?view=market_landscape&source=UBIST&measure=sales"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{API_BASE}/docs", wait_until="networkidle", timeout=20_000)
        result = page.evaluate(
            """
            async (path) => {
              try {
                const response = await fetch(path);
                const text = await response.text();
                return {
                  ok: response.ok,
                  status: response.status,
                  size: text.length,
                  bodyStart: text.slice(0, 120)
                };
              } catch (error) {
                return { error: String(error) };
              }
            }
            """,
            path,
        )
        browser.close()

    assert "error" not in result, result
    assert result["status"] == 200, result
    assert result["size"] > 1000, result
