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


def test_winnerf_a_plus_uses_1y_fallback_ei() -> None:
    """Pre-launch 5y CAGR should fall back to a 1y EI basis per PL definition."""
    brand = urllib.parse.quote("위너프A+")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")

    kpi = payload["data"]["kpi"]
    target = next(row for row in payload["data"]["ei_ms_matrix"]["data"] if row.get("is_target"))

    assert kpi["target_ei"] is not None
    assert kpi["ei_basis"] == "fallback_1y"
    assert kpi["ei_period_years"] == 1
    assert kpi["ei_note"] == "5년 전 매출 0 으로 1년 기준 계산"
    assert target["ei"] == kpi["target_ei"]
    assert target["ei_basis"] == "fallback_1y"
    assert target["cagr_5y_pct"] is None
    assert target["cagr_basis"] == "fallback_1y"


def test_standard_brand_keeps_5y_ei_basis() -> None:
    """Non-prelaunch brands keep the standard 5y EI calculation."""
    brand = urllib.parse.quote("라베칸")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=UBIST&measure=sales")

    kpi = payload["data"]["kpi"]

    assert kpi["target_ei"] is not None
    assert kpi["ei_basis"] == "standard_5y"
    assert kpi["ei_period_years"] == 5


def test_winnerf_a_plus_tooltip_uses_current_measure_value_and_label() -> None:
    """A.2 tooltip should show the backend row for the selected IQVIA measure."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{FRONTEND_URL}?phase20_nocache=1", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        page.select_option("#brand-select", value="위너프A+")
        page.click('.tab[data-page="cause"]')
        page.wait_for_timeout(2500)
        page.click('.view-tab[data-view="competitive_dynamics"]')
        page.wait_for_timeout(1500)

        page.click('.cause-measure-btn[data-measure="counting_unit"]')
        page.wait_for_timeout(2000)

        result = page.evaluate(
            """() => {
              const chart = charts['chart-ranking'];
              const dsIndex = chart.data.datasets.findIndex(d => d.label && d.label.startsWith('위너프A+'));
              const index = chart.data.labels.findIndex(y => Number(y) === 2024);
              const ctx = {
                chart,
                dataIndex: index,
                datasetIndex: dsIndex,
                dataset: chart.data.datasets[dsIndex],
                label: chart.data.labels[index],
                parsed: {y: chart.data.datasets[dsIndex].data[index]},
              };
              return chart.options.plugins.tooltip.callbacks.label(ctx);
            }"""
        )

        assert "위너프A+ ★: 0.57%" in result
        assert "  순위: #14" in result
        assert "  Counting Units: 8,050,856" in result
        assert "  매출: 0" not in result
        assert "  매출: 8,050,856" not in result
        browser.close()
