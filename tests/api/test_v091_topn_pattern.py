from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8013"


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def gardmet_cause() -> dict:
    brand = urllib.parse.quote("가드메트")
    return get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=UBIST&measure=sales")


def test_ei_ms_matrix_target_top5_no_others() -> None:
    """B.1: target + top 5, no 기타 row."""
    payload = gardmet_cause()
    data = payload["data"]["ei_ms_matrix"]["data"]

    assert 1 <= len(data) <= 6
    assert sum(1 for row in data if row.get("is_target")) == 1
    assert not any(row.get("is_others") for row in data)


def test_growth_ms_matrix_target_top5_no_others() -> None:
    """B.2: target + top 5, no 기타 row."""
    payload = gardmet_cause()
    data = payload["data"]["growth_contribution_ms_matrix"]["data"]

    assert 1 <= len(data) <= 6
    assert sum(1 for row in data if row.get("is_target")) == 1
    assert not any(row.get("is_others") for row in data)


def test_brand_ranking_top5_plus_optional_others() -> None:
    """A.2 keeps the ranking pattern target + top 5 + 기타 when residual brands exist."""
    payload = gardmet_cause()
    yearly = payload["data"]["brand_ranking_stacked"]["yearly"]
    assert yearly
    last = yearly[-1]["rankings"]

    assert 1 <= len(last) <= 7
    assert sum(1 for row in last if row.get("is_target")) == 1
    assert sum(1 for row in last if row.get("is_others")) <= 1


def test_matrix_avg_matches_visible_entries() -> None:
    """ms/share avg is calculated over displayed brand entries, excluding 기타."""
    payload = gardmet_cause()

    for chart_name in ("ei_ms_matrix", "growth_contribution_ms_matrix"):
        chart = payload["data"][chart_name]
        visible = [row for row in chart["data"] if not row.get("is_others")]
        expected = sum(float(row.get("share_pct") or 0) for row in visible) / len(visible)
        assert abs(float(chart["share_avg_pct"]) - expected) < 0.01, chart_name
        assert abs(float(chart["ms_avg_pct"]) - expected) < 0.01, chart_name


def test_hhi_series_filled_for_a3() -> None:
    payload = gardmet_cause()
    hhi = payload["data"]["sources_data"]["hhi_series_5y"]

    assert len(hhi) >= 3
    for point in hhi:
        assert point["year"]
        assert point["hhi"] > 0


def test_company_concentration_filled_for_a5() -> None:
    payload = gardmet_cause()
    trend = payload["data"]["company_concentration_trend"]

    assert len(trend["periods"]) >= 3
    assert len(trend["periods"]) == len(trend["hhi_values"])
    assert all(value > 0 for value in trend["hhi_values"])


def test_growth_contribution_filled_for_d1() -> None:
    payload = gardmet_cause()
    contribution = payload["data"]["growth_contribution"]

    assert contribution["by_brand"]["top_contributors"]
    assert contribution["by_company"]["top_contributors"]


def test_target_customer_competition_filled_for_d2() -> None:
    payload = gardmet_cause()
    competition = payload["data"]["target_customer_competition"]

    assert competition == payload["data"]["level_top5_trend"]
    assert competition["available_levels"]
    assert competition["by_level"]


def test_level_top5_trend_filled_for_d3() -> None:
    payload = gardmet_cause()
    trend = payload["data"]["level_top5_trend"]

    assert trend["available_levels"]
    assert trend["by_level"]
    for level in trend["available_levels"]:
        level_data = trend["by_level"][level["key"]]
        assert level_data["periods_10pt"]
        if level_data.get("empty"):
            assert level_data["values"] == []
            assert level_data["total_market_value"] == 0
            continue
        assert level_data["values"]


def test_market_status_undocumented_fields_removed() -> None:
    payload = get_api("/api/market-status")
    for card in payload["brand_cards"]:
        assert "brand_key" not in card, card["brand"]
        assert "source_cards" not in card, card["brand"]
        assert "market_label_kor" not in card, card["brand"]
        assert card["back_extended"]["market_label_kor"], card["brand"]
