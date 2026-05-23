from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8013"
FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def test_market_status_has_four_yoy_fields() -> None:
    """PL 13b Issue 1: KPI YoY variants are explicit and not placeholder zeros."""
    payload = get_api("/api/market-status")

    for source in ("UBIST", "IQVIA"):
        kpi = payload["kpi_summary"][source]
        for field in ("gr_yoy_pct", "gr_yoy_mat_pct", "gr_yoy_ym_pct", "ms_change_yoy_pct"):
            assert field in kpi, f"{source}: missing {field}"
            assert kpi[field] is not None, f"{source}: {field} should be computed or explicitly null only when impossible"

    assert payload["kpi_summary"]["UBIST"]["gr_yoy_mat_pct"] != 0
    assert payload["kpi_summary"]["IQVIA"]["gr_yoy_mat_pct"] != 0


def test_brand_cards_have_full_yoy_variants() -> None:
    payload = get_api("/api/market-status")

    for brand in ("라베칸", "가드메트"):
        card = next(card for card in payload["brand_cards"] if card["brand"] == brand)
        for field in ("gr_yoy_pct", "gr_yoy_mat_pct", "gr_yoy_ym_pct", "ms_change_yoy_pct"):
            assert card["front"][field] is not None, f"{brand}: {field}"
        assert card["front"]["gr_yoy_mat_pct"] != 0


def test_a1_market_yoy_series_has_nonzero_values() -> None:
    brand = urllib.parse.quote("가드메트")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=UBIST&measure=sales")
    series = payload["data"]["sources_data"]["market_yoy_series"]
    values = list(series.values()) if isinstance(series, dict) else series

    assert len(values) > 0
    assert sum(1 for value in values if value not in (None, 0)) >= 5


def test_rank_denominator_uses_full_market_count() -> None:
    payload = get_api("/api/market-status")
    card = next(card for card in payload["brand_cards"] if card["brand"] == "가드메트")

    assert card["rank"] == 3
    assert card["total_brands_in_market"] >= 100
    assert card["total_brands_in_market"] != 6


def test_frontend_static_has_no_unit_suffixes_or_display_rank_denominator() -> None:
    html = urllib.request.urlopen(FRONTEND_URL, timeout=30).read().decode("utf-8")

    for literal in ("원</", "억원", "조원", "백만원", "(₩)", "KRW", "처방건수", "eiData(ca).length"):
        assert literal not in html
    assert "total_brands_in_market" in html


def test_frontend_rank_and_units_render_without_won_suffix() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=30000)
        page.click("text=원인 분석")
        page.wait_for_timeout(2000)

        body = page.locator("body").inner_text()
        assert "#3 / 607" in body
        assert re.search(r"#3 / 6(?!\d)", body) is None
        assert not re.search(r"\d[\d,\\.]*[ \t]*원", body)

        app_errors = [error for error in console_errors if "pretendard" not in error.lower() and "font" not in error.lower()]
        assert app_errors == []
        browser.close()
