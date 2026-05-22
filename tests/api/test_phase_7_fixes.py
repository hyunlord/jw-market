"""Phase 7 v0.9.0 response contract and PL issue regression tests."""

from __future__ import annotations

import urllib.parse

import requests


BASE_URL = "http://127.0.0.1:8013"


def api_get(path: str) -> dict:
    response = requests.get(f"{BASE_URL}{path}", timeout=30)
    assert response.status_code == 200, response.text[:500]
    return response.json()


def cause(brand: str, source: str = "UBIST", measure: str = "sales") -> dict:
    encoded = urllib.parse.quote(brand)
    return api_get(f"/api/cause/{encoded}?view=market_landscape&source={source}&measure={measure}")


def deep(brand: str) -> dict:
    return api_get(f"/api/deep-analysis/{urllib.parse.quote(brand)}")


def test_market_status_multi_source_sources_data_split() -> None:
    data = api_get("/api/market-status")
    guard = next(card for card in data["brand_cards"] if card["brand"] == "가드렛")
    sources_data = guard["front"]["sources_data"]
    assert "UBIST" in sources_data
    assert "IQVIA" in sources_data
    assert sources_data["UBIST"]["value_recent"] is not None
    assert sources_data["IQVIA"]["value_recent"] is not None


def test_market_status_cagr_has_four_decimal_numeric_values() -> None:
    data = api_get("/api/market-status")
    for source in ("ubist", "iqvia"):
        value = data["kpi"][source]["cagr_5y_pct"]
        assert isinstance(value, (int, float))
        assert abs(value) < 1000
        assert len(str(value).split(".")[-1]) <= 4


def test_cause_spec_v0_9_required_keys() -> None:
    data = cause("리바로")
    required = {
        "kpi",
        "sources_data",
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
    assert required.issubset(data["data"].keys())
    assert "brand_ranking" not in data["data"]
    assert "company_ranking" not in data["data"]


def test_cause_rankings_include_others_bucket() -> None:
    data = cause("리바로")
    rankings = data["data"]["brand_ranking_stacked"]["yearly"][-1]["rankings"]
    assert any(row.get("is_target") for row in rankings)
    assert any(row.get("is_others") for row in rankings)
    assert any(row.get("brand") == "기타" for row in rankings)


def test_ml_003_uses_catalog_market_members_in_strategy_view() -> None:
    data = cause("가드메트")
    assert data["view"] == "market_landscape"
    assert data["data"]["kpi"]["direct_competition_count"] > 2
    rankings = data["data"]["brand_ranking_stacked"]["yearly"][-1]["rankings"]
    assert len(rankings) >= 7
    assert any(row.get("is_others") for row in rankings)


def test_deep_analysis_ai_events_and_history_only_forecast() -> None:
    data = deep("리바로")
    assert data["data"]["ai_analysis"]["phenomenon"]["title"]
    assert len(data["data"]["events"]) > 0
    by_combo = data["data"]["forecast"]["by_combo"]
    assert by_combo
    for combo in by_combo.values():
        assert combo["forecast_periods"] == []
        for brand_entry in combo["brands"]:
            assert brand_entry["history_values"]
            assert brand_entry["forecast_values"] == []
    for combo in data["data"]["simulation"]["by_combo"].values():
        assert combo["available_brands"]
        for brand_payload in combo["by_brand"].values():
            assert brand_payload["forecast_periods"] == []
            assert brand_payload["scenarios"] == {}
