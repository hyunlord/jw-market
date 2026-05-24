from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8013"
FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def get_api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def test_winnerf_a_plus_2024_backend_target_value_is_nonzero() -> None:
    """PL curl regression: backend target row has real 2024 value/MS, not zero."""
    brand = urllib.parse.quote("위너프A+")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")

    year_2024 = next(year for year in payload["data"]["brand_ranking_stacked"]["yearly"] if year["year"] == 2024)
    target = next(row for row in year_2024["rankings"] if row["brand"] == "위너프A+")

    assert target["value"] == 268755344
    assert target["rank"] == 14
    assert target["ms_pct"] == 0.6116


def test_winnerf_a_plus_2024_frontend_chart_uses_backend_target_row() -> None:
    """A.2 chart data must use the backend target row, not a synthetic zero row."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{FRONTEND_URL}?phase19_nocache=1", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)

        page.select_option("#brand-select", value="위너프A+")
        page.click('.tab[data-page="cause"]')
        page.wait_for_timeout(3000)
        page.click('.view-tab[data-view="competitive_dynamics"]')
        page.wait_for_timeout(3000)

        result = page.evaluate(
            """() => {
              const bi = ALL_BRANDS.find(b => b.brand === '위너프A+');
              const ca = ALL_DATA[bi.market_id]?.cause_analysis?.by_view_source?.competitive_dynamics?.IQVIA;
              const year = ca?.brand_ranking_stacked?.yearly?.find(y => Number(y.year) === 2024);
              const target = year?.rankings?.find(r => r.brand === '위너프A+');
              const chart = charts['chart-ranking'];
              const dataset = chart?.data?.datasets?.find(d => d.label && d.label.startsWith('위너프A+'));
              const yearIndex = chart?.data?.labels?.findIndex(y => Number(y) === 2024);
              return {
                targetValue: target?.value,
                targetMs: target?.ms_pct,
                chartMs: dataset?.data?.[yearIndex],
              };
            }"""
        )

        assert result["targetValue"] == 268755344
        assert result["targetMs"] == 0.6116
        assert result["chartMs"] == 0.6116
        browser.close()


def test_prelaunch_cagr_is_null_and_ei_uses_explicit_fallback() -> None:
    """Pre-launch brands keep 5y CAGR null while EI uses the explicit Phase 20 fallback."""
    brand = urllib.parse.quote("위너프A+")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")

    target = next(row for row in payload["data"]["ei_ms_matrix"]["data"] if row.get("is_target"))

    assert target["cagr_5y_pct"] is None
    assert target["ei"] is not None
    assert target["ei_5y"] is not None
    assert target["ei_basis"] == "fallback_1y"
