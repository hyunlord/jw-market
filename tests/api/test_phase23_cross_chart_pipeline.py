from __future__ import annotations

import json
import math
import subprocess
import urllib.parse
import urllib.request
from typing import Any

from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:8013"
FRONTEND_URL = "http://127.0.0.1:8888/jw_market_hardcoded_mockup_v3_4.html"


def get_api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def cause_payload(brand: str, *, view: str, source: str, measure: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(brand)
    return get_api(f"/api/cause/{encoded}?view={view}&source={source}&measure={measure}")


def test_hemlibra_iqvia_sales_cross_chart_share_is_not_counting_unit() -> None:
    """B.1/B.2/A.2 must all stay on sales when measure=sales."""
    payload = cause_payload("헴리브라", view="competitive_dynamics", source="IQVIA", measure="sales")
    data = payload["data"]

    latest = data["brand_ranking_stacked"]["yearly"][-1]
    a2 = next(row for row in latest["rankings"] if row.get("is_target"))
    b1 = next(row for row in data["ei_ms_matrix"]["data"] if row.get("is_target"))
    b2 = next(row for row in data["growth_contribution_ms_matrix"]["data"] if row.get("is_target"))
    kpi = data["kpi"]

    for value in (a2["ms_pct"], b1["ms_pct"], b2["ms_pct"], kpi["target_share_pct"]):
        assert math.isclose(float(value), 46.4555, rel_tol=0, abs_tol=0.01)
        assert not math.isclose(float(value), 3.8854, rel_tol=0, abs_tol=0.01)


def test_hemlibra_d2_iqvia_channel_tabs_are_distinct() -> None:
    """D.2 channel tabs must be channel-filtered, not copies of 전체."""
    payload = cause_payload("헴리브라", view="competitive_dynamics", source="IQVIA", measure="sales")
    views = payload["data"]["target_customer_competition_by_channel"]["views"]
    signatures = {
        view["target_name"]: tuple((row["brand"], round(float(row["pct"]), 4)) for row in view["composition"])
        for view in views
    }

    assert {"전체", "KHPA", "KCPA", "KPA"}.issubset(signatures)
    assert len(set(signatures.values())) > 1, signatures


def test_hemlibra_d3_class_totals_are_real_segment_totals() -> None:
    """D.3 Class bars must use each class total, not the same market total for every class."""
    payload = cause_payload("헴리브라", view="competitive_dynamics", source="IQVIA", measure="sales")
    class_values = payload["data"]["level_top5_trend"]["by_level"]["Class"]["values"]

    totals = [round(float(row["total_value"]), 2) for row in class_values]
    shares = [round(float(row["ms_pct"]), 4) for row in class_values]

    assert len(class_values) >= 4
    assert len(set(totals)) == len(totals), class_values
    assert len(set(shares)) == len(shares), class_values

    non_factor = next(row for row in class_values if row["value"] == "Non-Factor")
    hemlibra = next(row for row in non_factor["brands_in_value"] if row["brand"] == "헴리브라")
    assert hemlibra["ms_recent_pct"] > 90


def test_target_customer_competition_copies_level_top5_trend() -> None:
    payload = cause_payload("헴리브라", view="competitive_dynamics", source="IQVIA", measure="sales")
    data = payload["data"]

    assert data["target_customer_competition"] == data["level_top5_trend"]


def test_frontend_sales_b1_is_not_overwritten_by_counting_unit_merge() -> None:
    """The frontend cache must not merge counting_unit neutral fields over sales fields."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{FRONTEND_URL}?phase23_nocache=1", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)

        page.select_option("#brand-select", value="헴리브라")
        page.click('.tab[data-page="cause"]')
        page.wait_for_timeout(2500)
        page.click('.view-tab[data-view="competitive_dynamics"]')
        page.wait_for_timeout(1500)

        result = page.evaluate(
            """() => {
              const chart = charts['chart-ei-ms'];
              const dsIndex = chart.data.datasets.findIndex(d => d.label === '헴리브라');
              return {
                currentMeasure: window.causeMeasure,
                x: chart.data.datasets[dsIndex].data[0].x,
                y: chart.data.datasets[dsIndex].data[0].y,
              };
            }"""
        )

        assert result["currentMeasure"] == "sales"
        assert math.isclose(result["x"], 46.4555, rel_tol=0, abs_tol=0.01)
        assert not math.isclose(result["x"], 3.8854, rel_tol=0, abs_tol=0.01)
        browser.close()


def test_phase23_consistency_pipeline_passes() -> None:
    """The forced all-brand/view/source/measure validation pipeline must be clean."""
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase23_consistency_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
