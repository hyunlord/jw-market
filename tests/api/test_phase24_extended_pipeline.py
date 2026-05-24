from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "http://127.0.0.1:8013"
HTML_PATH = Path("docs/reference/jw_market_hardcoded_mockup_v3_4.html")


def get_api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def cause_payload(brand: str, *, view: str, source: str, measure: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(brand)
    return get_api(f"/api/cause/{encoded}?view={view}&source={source}&measure={measure}")["data"]


def test_deep_analysis_forecast_values_are_populated() -> None:
    """Forecast tab must have actual forecast_values, not only historical lines."""
    payload = get_api(f"/api/deep-analysis/{urllib.parse.quote('리바로')}")
    by_combo = payload["data"]["forecast"]["by_combo"]
    forecast_counts = [
        len(brand.get("forecast_values") or [])
        for combo in by_combo.values()
        for brand in combo.get("brands") or []
    ]

    assert forecast_counts
    assert min(forecast_counts) > 0


def test_d3_options_include_all_values_and_atomic_doses() -> None:
    """D.3 dropdowns must expose every option and split strength packs atomically."""
    data = cause_payload("리바로", view="market_landscape", source="UBIST", measure="sales")
    by_level = data["level_top5_trend"]["by_level"]

    assert "Brand" not in by_level
    assert by_level
    for level, payload in by_level.items():
        assert len(payload["values"]) == len(payload["all_options"]), level
        assert len(payload["all_options"]) > 1, level

    dose_options = by_level["용량"]["all_options"]
    assert dose_options
    assert not any("|" in str(option) for option in dose_options), dose_options[:10]


def test_frontend_d2_d3_measure_controls_are_source_specific() -> None:
    """D.2/D.3 controls should not be hard-coded to UBIST-only volume."""
    html = HTML_PATH.read_text(encoding="utf-8")

    assert "data-target=\"d2\">처방량 <span class=\"ubist-only-hint\">(UBIST 만)</span>" not in html
    assert "data-target=\"d3\">처방량 <span class=\"ubist-only-hint\">(UBIST 만)</span>" not in html
    assert "renderSectionMeasureButtons" in html
    assert "Counting Units (IQVIA)" in html


def test_target_customer_periods_remain_ten_points_with_sparse_note() -> None:
    """Sparse channels may have zero values, but must expose 10 points and an explicit note."""
    data = cause_payload("리바로", view="competitive_dynamics", source="UBIST", measure="sales")
    d2 = data["target_customer_competition"]
    jong = next(view for view in d2["views"] if view["target_name"] == "종병")

    assert len(jong["periods"]) == 10
    assert "data_quality" in jong
    assert jong["data_quality"]["period_count"] == 10
    assert jong["data_quality"]["nonzero_period_count"] < 10
    assert "2026-01" in (jong["data_quality"].get("note") or "")


def test_phase24_extended_pipeline_passes() -> None:
    """Phase 24 validator extends Phase 23 and must pass cleanly."""
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase24_extended_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=360,
    )
    assert result.returncode == 0, result.stdout + result.stderr
