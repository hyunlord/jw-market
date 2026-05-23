from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pymysql


BASE_URL = "http://127.0.0.1:8013"


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def gardmet_cause() -> dict:
    brand = urllib.parse.quote("가드메트")
    return get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=UBIST&measure=sales")


def test_strategic_mart_collapses_livalozet_skus() -> None:
    """Issue 2/10: strategic marts must use brand grain, not product SKU grain."""
    conn = pymysql.connect(
        host="localhost",
        port=3308,
        user="root",
        password="<LOCAL_ROOT_PW>",
        database="jw_mart",
    )
    try:
        cur = conn.cursor()
        for table in ("mart_strategic_ml_brand_metric", "mart_strategic_cd_brand_metric"):
            cur.execute(
                f"""
                SELECT DISTINCT brand_name
                FROM {table}
                WHERE brand_name LIKE '리바로젯%'
                ORDER BY brand_name
                """
            )
            assert [row[0] for row in cur.fetchall()] == ["리바로젯"], table
    finally:
        conn.close()


def test_market_status_exposes_yoy_and_full_market_rank_denominator() -> None:
    """Issue 1/7: YoY is present and rank denominator is the full market brand count."""
    payload = get_api("/api/market-status")

    assert payload["kpi_summary"]["UBIST"]["avg_yoy_pct"] is not None
    assert payload["kpi_summary"]["IQVIA"]["avg_yoy_pct"] is not None

    card = next(card for card in payload["brand_cards"] if card["brand"] == "가드메트")
    assert card["rank"] >= 1
    assert card["total_brands_in_market"] > 6
    assert card["total_brands_in_market"] >= card["rank"]


def test_a2_market_share_closes_to_100_with_others() -> None:
    """Issue 3: target + top 5 + 기타 should represent the whole market."""
    payload = gardmet_cause()
    latest = payload["data"]["brand_ranking_stacked"]["yearly"][-1]["rankings"]

    assert any(row.get("is_others") for row in latest)
    total = sum(float(row.get("ms_pct") or 0.0) for row in latest)
    assert abs(total - 100.0) < 0.5


def test_cause_market_yoy_series_is_filled() -> None:
    """Issue 1: A.1 has a YoY series instead of a silent zero/default."""
    payload = gardmet_cause()
    sources = payload["data"]["sources_data"]

    assert sources["market_yoy_recent_pct"] is not None
    assert sources["market_yoy_series"]


def test_d1_waterfall_contribution_closes_to_100() -> None:
    """Issue 9: D.1 waterfall contribution rows decompose market growth."""
    payload = gardmet_cause()
    contribution = payload["data"]["growth_contribution"]

    for key in ("by_brand", "by_company"):
        rows = contribution[key]["top_contributors"]
        assert rows
        total = sum(float(row.get("contribution_pct") or 0.0) for row in rows)
        assert abs(total - 100.0) < 0.5, key


def test_d3_level_comparison_has_distinct_option_lists() -> None:
    """Issue 11: D.3 exposes level-specific option lists instead of one repeated payload."""
    payload = gardmet_cause()
    by_level = payload["data"]["level_top5_trend"]["by_level"]

    option_sets = {}
    for level, level_data in by_level.items():
        options = level_data.get("all_options")
        assert options, level
        option_sets[level] = tuple(options)

    assert len(set(option_sets.values())) > 1


def test_response_values_are_raw_numbers_not_unit_strings() -> None:
    """Issue 6/12: numeric fields remain numeric raw values; unit text is presentation only."""
    payload = get_api("/api/market-status")
    numeric_values = []
    for card in payload["brand_cards"]:
        numeric_values.extend(
            [
                card["front"]["value_recent"],
                card["back_extended"]["market_size_recent"],
            ]
        )

    assert all(isinstance(value, (int, float)) for value in numeric_values)
    assert any(value and value > 1_000_000 for value in numeric_values)


def test_forecast_empty_keeps_simulation_scenarios_empty() -> None:
    """Issue 13: backend does not fabricate forecast/simulation values."""
    brand = urllib.parse.quote("가드메트")
    payload = get_api(f"/api/deep-analysis/{brand}")

    for combo in payload["data"]["forecast"]["by_combo"].values():
        for brand_row in combo["brands"]:
            assert brand_row["forecast_values"] == []

    for combo in payload["data"]["simulation"]["by_combo"].values():
        for brand_row in combo["by_brand"].values():
            assert brand_row["scenarios"]["base"]["values"] == []
            assert brand_row["scenarios"]["upper"]["values"] == []
            assert brand_row["scenarios"]["lower"]["values"] == []
            assert brand_row["confidence"]["score"] is None
