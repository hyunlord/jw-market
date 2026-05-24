from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8013"


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def cause_payload(brand: str, *, view: str, source: str, measure: str) -> dict:
    encoded = urllib.parse.quote(brand)
    return get_api(f"/api/cause/{encoded}?view={view}&source={source}&measure={measure}")


def assert_matrix_shares_match_market_size(payload: dict) -> None:
    data = payload["data"]
    market_size = float(data["kpi"]["market_size_recent"])
    assert market_size > 0

    for chart_name in ("ei_ms_matrix", "growth_contribution_ms_matrix"):
        chart = data[chart_name]
        visible = [row for row in chart["data"] if not row.get("is_others")]
        assert visible, chart_name
        expected_avg = 0.0
        for row in visible:
            value = float(row.get("value_recent") or 0)
            expected_share = value / market_size * 100
            expected_avg += expected_share
            assert abs(float(row["share_pct"]) - expected_share) < 0.01, (chart_name, row["brand"])
            assert abs(float(row["ms_pct"]) - expected_share) < 0.01, (chart_name, row["brand"])
        expected_avg /= len(visible)
        assert abs(float(chart["share_avg_pct"]) - expected_avg) < 0.01, chart_name
        assert abs(float(chart["ms_avg_pct"]) - expected_avg) < 0.01, chart_name

    target = next(row for row in data["ei_ms_matrix"]["data"] if row.get("is_target"))
    assert abs(float(data["kpi"]["target_share_pct"]) - float(target["share_pct"])) < 0.01
    assert abs(float(data["kpi"]["brand_share_pct"]) - float(target["share_pct"])) < 0.01
    assert data["kpi"]["target_rank"] == target["rank"]


def test_hemlibra_b1_b2_sales_shares_use_full_market_denominator() -> None:
    """헴리브라 B.1/B.2 sales M/S must be raw value / full market size, not stale mart ms."""
    payload = cause_payload("헴리브라", view="competitive_dynamics", source="IQVIA", measure="sales")

    matrix = payload["data"]["ei_ms_matrix"]["data"]
    hemlibra = next(row for row in matrix if row.get("brand") == "헴리브라")
    novoseven = next(row for row in matrix if row.get("brand") == "노보세븐알티")

    assert abs(float(hemlibra["share_pct"]) - 46.4555) < 0.01
    assert abs(float(novoseven["share_pct"]) - 6.5572) < 0.01
    assert_matrix_shares_match_market_size(payload)


def test_hemlibra_b1_b2_counting_unit_shares_use_full_market_denominator() -> None:
    """PL screenshot case: counting_unit should show 헴리브라 around 3.89%, not stale 4.06%."""
    payload = cause_payload("헴리브라", view="competitive_dynamics", source="IQVIA", measure="counting_unit")

    matrix = payload["data"]["ei_ms_matrix"]["data"]
    hemlibra = next(row for row in matrix if row.get("brand") == "헴리브라")

    assert abs(float(hemlibra["share_pct"]) - 3.8854) < 0.01
    assert_matrix_shares_match_market_size(payload)


def test_market_status_hemlibra_sales_share_matches_market_size() -> None:
    """Market-status card should use the same full-market denominator as B.1/B.2."""
    payload = get_api("/api/market-status")
    card = next(row for row in payload["brand_cards"] if row["brand"] == "헴리브라")

    value = float(card["front"]["value_recent"])
    market_size = float(card["back_extended"]["market_size_recent"])
    expected = value / market_size * 100

    assert abs(float(card["front"]["ms_recent_pct"]) - expected) < 0.01
    iqvia = card["front"]["sources_data"]["IQVIA"]
    assert abs(float(iqvia["ms_recent_pct"]) - expected) < 0.01
