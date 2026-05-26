from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8013"
EXPECTED_COMBOS = {
    "UBIST.sales",
    "UBIST.volume",
    "IQVIA.sales",
    "IQVIA.unit",
    "IQVIA.dosage_unit",
    "IQVIA.counting_unit",
}


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def test_market_status_envelope_matches_v091() -> None:
    payload = get_api("/api/market-status")

    assert set(payload.keys()) == {"kpi_summary", "brand_cards"}
    assert set(payload["kpi_summary"].keys()) == {"UBIST", "IQVIA"}
    assert len(payload["brand_cards"]) == 25


def test_market_status_kpi_fields_v091() -> None:
    payload = get_api("/api/market-status")
    required = {
        "total_sales_recent_krw",
        "avg_ms_per_brand_pct",
        "sales_up_count",
        "sales_down_count",
        "avg_cagr_5y_pct",
        "period_recent",
        "brand_count",
    }

    for source in ("UBIST", "IQVIA"):
        keys = set(payload["kpi_summary"][source].keys())
        assert required <= keys
        assert payload["kpi_summary"][source]["avg_yoy_pct"] is not None
        assert payload["kpi_summary"][source]["period_recent"]


def test_cause_analysis_levels_measure_neutral_value_series() -> None:
    brand = urllib.parse.quote("가드메트")
    payload = get_api(f"/api/cause/{brand}?view=market_landscape&source=IQVIA&measure=dosage_unit")

    required_data_keys = {
        "kpi",
        "sources_data",
        "market_size_series",
        "hhi_series_5y",
        "hhi_recent",
        "brand_ranking",
        "company_ranking",
        "brand_ranking_stacked",
        "company_ranking_stacked",
        "company_concentration_trend",
        "ei_ms_matrix",
        "growth_contribution_ms_matrix",
        "analysis_levels",
        "growth_contribution",
        "target_customer_competition",
        "level_top5_trend",
    }
    assert required_data_keys <= set(payload["data"].keys())

    analysis_levels = payload["data"]["analysis_levels"]
    level = analysis_levels["levels"][0]
    channel = analysis_levels["channels"][0]
    segment = analysis_levels["data"][level]["by_channel"][channel][0]

    assert set(segment.keys()) == {"name", "rank", "recent_share_pct", "series_pct", "value_series"}
    assert isinstance(segment["series_pct"], list)
    assert isinstance(segment["value_series"], list)
    assert segment["value_series"]


def test_deep_analysis_has_six_v091_combos() -> None:
    brand = urllib.parse.quote("가드메트")
    payload = get_api(f"/api/deep-analysis/{brand}")

    assert set(payload["data"]["forecast"]["by_combo"].keys()) == EXPECTED_COMBOS
    assert set(payload["data"]["simulation"]["by_combo"].keys()) == EXPECTED_COMBOS


def test_deep_analysis_forecast_history_only() -> None:
    brand = urllib.parse.quote("가드메트")
    payload = get_api(f"/api/deep-analysis/{brand}")

    for combo_key, combo in payload["data"]["forecast"]["by_combo"].items():
        assert combo["brands"], combo_key
        for brand_entry in combo["brands"]:
            assert "history_values" in brand_entry
            assert isinstance(brand_entry["history_values"], list)
            assert brand_entry["history_values"], combo_key
            assert isinstance(brand_entry["forecast_values"], list)
            assert brand_entry["forecast_values"], combo_key
            assert len(brand_entry["forecast_values"]) == len(combo["forecast_periods"])


def test_deep_analysis_mock_sections_v091() -> None:
    brand = urllib.parse.quote("가드메트")
    payload = get_api(f"/api/deep-analysis/{brand}")

    events = payload["data"]["events"]
    assert set(events.keys()) >= {"cut_a", "cut_b", "meta"}
    assert isinstance(events["cut_a"], list)
    assert isinstance(events["cut_b"], list)
    assert isinstance(events["meta"], dict)
    assert set(payload["data"]["ai_analysis"].keys()) >= {
        "phenomenon",
        "cause",
        "prediction",
        "recommendation",
    }


def test_health_version_v091() -> None:
    payload = get_api("/api/health")
    assert payload["version"] == "v0.9.1"
