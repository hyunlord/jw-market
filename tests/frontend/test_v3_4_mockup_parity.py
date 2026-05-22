from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / "docs/reference/jw_market_hardcoded_mockup_20260520.html"
V34 = ROOT / "docs/reference/jw_market_hardcoded_mockup_v3_4.html"
URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def test_v3_4_preserves_original_mockup_structure() -> None:
    original = ORIGINAL.read_text()
    v34 = V34.read_text()

    assert sorted(re.findall(r'<canvas id="([^"]+)"', original)) == sorted(
        re.findall(r'<canvas id="([^"]+)"', v34)
    )
    assert sorted(re.findall(r'section-title-num">([A-D]\.\d+)<', original)) == sorted(
        re.findall(r'section-title-num">([A-D]\.\d+)<', v34)
    )


def test_v3_4_diff_is_adapter_only() -> None:
    v34 = V34.read_text()

    assert "const API_BASE = 'http://127.0.0.1:8013';" in v34
    assert "→ FETCH" in v34
    assert "adaptV091BrandCards" in v34
    assert "window.__KPI_SUMMARY__" in v34


def test_v3_4_loads_backend_data_in_original_layout() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        api_calls: list[str] = []
        console_errors: list[str] = []
        page.on("request", lambda request: api_calls.append(request.url) if "/api/" in request.url else None)
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error" and "Refused to apply style" not in message.text
            else None,
        )

        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(7000)

        assert any("/api/market-status" in url for url in api_calls)
        assert console_errors == []
        assert page.locator(".kpi-card").count() == 5
        assert page.locator(".brand-card").count() == 25
        assert page.locator('canvas[id^="chart-"]').count() == 8

        browser.close()
