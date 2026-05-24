from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from typing import Any

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8013"
FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def get_api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def test_winnerf_a_plus_market_landscape_target_row_uses_mart_value() -> None:
    """ML view must not synthesize a zero row when the target is outside top 5."""
    brand = urllib.parse.quote("위너프A+")
    payload = get_api(f"/api/cause/{brand}?view=market_landscape&source=IQVIA&measure=sales")

    year_2024 = next(year for year in payload["data"]["brand_ranking_stacked"]["yearly"] if year["year"] == 2024)
    target = next(row for row in year_2024["rankings"] if row["brand"] == "위너프A+")

    assert target["value"] == 268755344
    assert target["rank"] == 33
    assert math.isclose(target["ms_pct"], 0.4445, rel_tol=0, abs_tol=0.0001)


def test_winnerf_a_plus_market_landscape_frontend_dataset_is_not_zero() -> None:
    """The ML A.2 chart dataset should contain the non-zero backend target point."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{FRONTEND_URL}?phase21_nocache=1", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)

        page.select_option("#brand-select", value="위너프A+")
        page.click('.tab[data-page="cause"]')
        page.wait_for_timeout(2500)
        page.click('.view-tab[data-view="market_landscape"]')
        page.wait_for_timeout(2000)

        result = page.evaluate(
            """() => {
              const chart = charts['chart-ranking'];
              const dsIndex = chart.data.datasets.findIndex(d => d.label && d.label.startsWith('위너프A+'));
              const yearIndex = chart.data.labels.findIndex(y => Number(y) === 2024);
              const ctx = {
                chart,
                dataIndex: yearIndex,
                datasetIndex: dsIndex,
                dataset: chart.data.datasets[dsIndex],
                label: chart.data.labels[yearIndex],
                parsed: {y: chart.data.datasets[dsIndex].data[yearIndex]},
              };
              return {
                datasetValue: chart.data.datasets[dsIndex].data[yearIndex],
                tooltip: chart.options.plugins.tooltip.callbacks.label(ctx),
              };
            }"""
        )

        assert math.isclose(result["datasetValue"], 0.4445, rel_tol=0, abs_tol=0.0001)
        assert "위너프A+ ★: 0.44%" in result["tooltip"]
        assert "  순위: #33" in result["tooltip"]
        assert "  매출: 268,755,344" in result["tooltip"]
        assert "  매출: 0" not in result["tooltip"]
        browser.close()


def test_winnerf_a_plus_prelaunch_ei_is_na_without_1y_fallback() -> None:
    """PL reversed Phase 20 fallback: pre-launch 5y EI should remain N/A."""
    brand = urllib.parse.quote("위너프A+")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")

    kpi = payload["data"]["kpi"]
    target = next(row for row in payload["data"]["ei_ms_matrix"]["data"] if row.get("is_target"))

    assert kpi["target_ei"] is None
    assert kpi["ei"] is None
    assert kpi["ei_basis"] == "unable"
    assert kpi["ei_note"] == "5년 전 매출 0 — N/A"
    assert target["ei"] is None
    assert target["ei_basis"] == "unable"
    assert target["cagr_5y_pct"] is None
    assert target["cagr_basis"] == "unable"


def test_standard_brand_keeps_standard_5y_ei_after_fallback_removal() -> None:
    """Removing 1y fallback must not break standard 5y EI brands."""
    brand = urllib.parse.quote("라베칸")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=UBIST&measure=sales")

    kpi = payload["data"]["kpi"]

    assert kpi["target_ei"] is not None
    assert kpi["ei_basis"] == "standard_5y"
    assert kpi["ei_period_years"] == 5
