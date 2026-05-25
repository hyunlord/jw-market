from __future__ import annotations

import json
import subprocess

import pymysql

from pipeline.scripts.etl.cache_build_common import CANONICAL_25


HORIZON_CI_LEVELS = {
    "1y": 0.95,
    "3y": 0.95,
    "5y": 0.95,
    "10y": 0.95,
    "method": "natural_accumulation_95_only",
    "note": "Phase 30.2: horizon 차등 제거, 모든 horizon 95% CI 자연 누적",
}


def _conn():
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _deep_payload(brand: str) -> dict:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT response_json FROM cache_deep_analysis WHERE brand=%s", [brand])
        row = cur.fetchone()
    finally:
        conn.close()
    assert row, brand
    return json.loads(row["response_json"])


def test_phase30_model_dispatch_policy() -> None:
    from pipeline.scripts.forecast.forecast_runner import select_model

    assert select_model(64, "UBIST").name == "Prophet"
    assert select_model(45, "UBIST").name == "SARIMAX"
    assert select_model(33, "UBIST").variant == "base"
    assert select_model(24, "UBIST").name == "HoltWinters"
    assert select_model(15, "UBIST").name == "Linear"
    assert select_model(8, "UBIST").name == "Mean"

    assert select_model(22, "IQVIA").name == "HoltWinters"
    assert select_model(15, "IQVIA").name == "Linear"
    assert select_model(8, "IQVIA").name == "Mean"


def test_phase30_cache_has_simulation_for_every_available_combo() -> None:
    for brand in ["리바로", "가드메트", "헴리브라"]:
        payload = _deep_payload(brand)
        available = set(payload["available_combos"])
        forecast_combos = set(payload["data"]["forecast"]["by_combo"])
        simulation_combos = set(payload["data"]["simulation"]["by_combo"])

        assert available
        assert forecast_combos == available
        assert simulation_combos == available


def test_phase30_simulation_schema_for_livalo_sales() -> None:
    payload = _deep_payload("리바로")
    sim = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]
    assert sim["period_unit"] == "월"
    assert sim["unit_label"] == "KRW"

    by_brand = sim["by_brand"]
    assert "리바로" in by_brand
    assert len(by_brand) >= 1

    target = by_brand["리바로"]
    assert target["horizon_ci_levels"] == HORIZON_CI_LEVELS
    assert len(target["forecast_periods"]) == 120
    assert len(target["scenarios"]["base"]["values"]) == 120
    assert len(target["scenarios"]["upper"]["values"]) == 120
    assert len(target["scenarios"]["lower"]["values"]) == 120
    assert target["model"]["selection_policy"] == "data_size_dispatch_v1"
    assert target["model"]["event_regressor"]["enabled"] is False
    assert target["confidence"]["method"] == "ci_width_normalized"
    assert 0 <= target["confidence"]["score"] <= 100
    assert target["market_comparison"]["method"] == "brand_cagr_minus_market_cagr_same_source"
    assert target["momentum"]["method"] == "forecast_slope_avg"
    assert "anomaly_signals" not in target
    assert "stress" not in target
    assert isinstance(target["warnings"], list)
    assert target["baseline"]["value_recent"] is not None


def test_phase30_iqvia_quarterly_horizon_lengths() -> None:
    payload = _deep_payload("헴리브라")
    target = payload["data"]["simulation"]["by_combo"]["IQVIA.sales"]["by_brand"]["헴리브라"]
    assert payload["data"]["simulation"]["by_combo"]["IQVIA.sales"]["period_unit"] == "분기"
    assert len(target["forecast_periods"]) == 40
    assert len(target["scenarios"]["base"]["values"]) == 40


def test_phase30_all_canonical_brands_have_forecast_and_simulation_payloads() -> None:
    for brand in CANONICAL_25:
        payload = _deep_payload(brand)
        assert payload["data"]["ai_analysis"] == {}
        available = payload["available_combos"]
        assert available, brand
        for combo in available:
            forecast_combo = payload["data"]["forecast"]["by_combo"][combo]
            sim_combo = payload["data"]["simulation"]["by_combo"][combo]
            assert forecast_combo["forecast_periods"], (brand, combo)
            assert sim_combo["by_brand"], (brand, combo)
            for sim_brand, sim_data in sim_combo["by_brand"].items():
                assert sim_data["model"]["event_regressor"]["enabled"] is False, (brand, combo, sim_brand)
                assert sim_data["horizon_ci_levels"] == HORIZON_CI_LEVELS, (brand, combo, sim_brand)


def test_phase30_validation_pipeline_passes() -> None:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase30_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
