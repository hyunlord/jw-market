from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "docs/reference/jw_market_hardcoded_mockup_v3_4.html"
URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def html() -> str:
    return PATH.read_text()


def strip_comments(source: str) -> str:
    source = re.sub(r"//[^\n]*", "", source)
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)


def test_v3_4_no_mock_data_variable() -> None:
    source = html()

    assert "window.__MOCK_DATA__ = {" not in source
    assert "window.__MOCK_DATA__ = [" not in source
    assert re.findall(r"window\.__MOCK_DATA__|__MOCK_DATA__", strip_comments(source)) == []


def test_v3_4_size_reduced_after_mock_payload_removal() -> None:
    size_kb = os.path.getsize(PATH) / 1024
    assert size_kb < 500, f"v3.4 still looks like it contains embedded mock data: {size_kb:.0f} KB"


def test_v3_4_html_structure_preserved() -> None:
    source = html()

    assert len(re.findall(r'<canvas id="([^"]+)"', source)) == 14
    assert set(re.findall(r'section-title-num">([A-D]\.\d+)<', source)) == {
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
    }


def test_v3_4_adapter_and_error_handler_preserved() -> None:
    source = html()

    assert "const API_BASE = (() => {" in source
    assert "return origin + pathname;" in source
    assert "window.location.port === '8888'" in source
    assert "adaptV091BrandCards" in source
    assert "__KPI_SUMMARY__" in source
    assert "showApiError" in source
    assert "api-error-banner" in source


def test_v3_4_backend_failure_shows_error_banner() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("http://127.0.0.1:8013/api/**", lambda route: route.abort())
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        banner = page.locator("#api-error-banner")
        assert banner.count() == 1
        assert "No mock fallback exists" in banner.inner_text()
        assert page.locator(".brand-card").count() == 0

        browser.close()
