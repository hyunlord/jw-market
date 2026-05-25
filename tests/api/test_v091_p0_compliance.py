from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8013"
EXPECTED_DUAL_COMBOS = {
    "UBIST.sales",
    "UBIST.volume",
    "IQVIA.sales",
    "IQVIA.unit",
    "IQVIA.dosage_unit",
    "IQVIA.counting_unit",
}
EXPECTED_CHANNELS = ["전체", "상급종병", "종병", "병원", "의원/보건소"]
EXPECTED_CAUSE_LEVELS = ["Class", "Molecule", "Brand", "제형/투여경로", "용량", "비/급여", "Ox/Gx"]


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=60) as response:
        return json.load(response)


def cause(brand: str, *, source: str = "IQVIA", measure: str = "dosage_unit") -> dict:
    encoded = urllib.parse.quote(brand)
    return get_api(f"/api/cause/{encoded}?view=market_landscape&source={source}&measure={measure}")


def deep(brand: str) -> dict:
    return get_api(f"/api/deep-analysis/{urllib.parse.quote(brand)}")


def test_cause_analysis_levels_follow_market_ground_truth_for_ml003() -> None:
    payload = cause("가드메트", source="UBIST", measure="sales")
    analysis_levels = payload["data"]["analysis_levels"]

    assert analysis_levels["levels"] == EXPECTED_CAUSE_LEVELS
    assert analysis_levels["channels"] == EXPECTED_CHANNELS


def test_cause_analysis_levels_have_korean_period_units_and_period_lists() -> None:
    ubist = cause("가드메트", source="UBIST", measure="sales")["data"]["analysis_levels"]
    iqvia = cause("가드메트", source="IQVIA", measure="sales")["data"]["analysis_levels"]

    assert ubist["period_unit"] == "월"
    assert len(ubist["periods_monthly"]) >= 60
    assert ubist["periods_quarterly"] == []

    assert iqvia["period_unit"] == "분기"
    assert iqvia["periods_monthly"] == []
    assert len(iqvia["periods_quarterly"]) >= 20


def test_cause_analysis_level_segments_are_populated_for_all_channels() -> None:
    analysis_levels = cause("가드메트", source="UBIST", measure="sales")["data"]["analysis_levels"]
    level = analysis_levels["levels"][0]

    for channel in EXPECTED_CHANNELS:
        segments = analysis_levels["data"][level]["by_channel"][channel]
        assert segments, f"{level}/{channel} should have segments"
        segment = segments[0]
        assert set(segment.keys()) == {"name", "rank", "recent_share_pct", "series_pct", "value_series"}
        assert len(segment["series_pct"]) == len(analysis_levels["periods_monthly"])
        assert len(segment["value_series"]) == len(analysis_levels["periods_monthly"])


def test_cause_ml011_splits_class_into_class_1_and_class_2() -> None:
    payload = cause("악템라", source="IQVIA", measure="sales")
    levels = payload["data"]["analysis_levels"]["levels"]

    assert "Class 1" in levels
    assert "Class 2" in levels
    assert "Class" not in levels


def test_deep_available_combos_for_dual_market() -> None:
    payload = deep("가드메트")

    assert set(payload["available_combos"]) == EXPECTED_DUAL_COMBOS
    assert set(payload["market_meta"]["available_combos"]) == EXPECTED_DUAL_COMBOS


def test_deep_forecast_has_target_plus_top5_and_future_periods_only() -> None:
    payload = deep("가드메트")

    for combo_key, combo in payload["data"]["forecast"]["by_combo"].items():
        assert len(combo["brands"]) == 6, combo_key
        assert combo["brands"][0]["is_target"] is True
        assert all(brand["forecast_values"] == [] for brand in combo["brands"])
        if combo_key.startswith("UBIST."):
            assert len(combo["forecast_periods"]) == 120
        else:
            assert len(combo["forecast_periods"]) == 40


def test_deep_simulation_policy_and_history_fields_are_populated() -> None:
    payload = deep("가드메트")

    for combo_key, combo in payload["data"]["simulation"]["by_combo"].items():
        assert len(combo["available_brands"]) == 6, combo_key
        first_entry = next(iter(combo["by_brand"].values()))
        assert first_entry["horizon_ci_levels"] == {"1y": 0.95, "3y": 0.9, "5y": 0.8, "10y": 0.5}
        assert {"delta_pp", "brand_cagr_pct", "market_cagr_pct", "basis", "horizon", "method"} <= set(
            first_entry["market_comparison"]
        )
        assert first_entry["market_comparison"]["method"] == "history_only"
        assert first_entry["momentum"]["label"] in {"stable", "rising", "declining", "insufficient_data"}
        assert first_entry["momentum"]["method"] == "trailing_mean"
        assert first_entry["anomaly_signals"]["method"] == "yoy_threshold"
        assert isinstance(first_entry["anomaly_signals"]["items"], list)
