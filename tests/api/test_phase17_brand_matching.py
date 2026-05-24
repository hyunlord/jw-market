from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8013"


def get_api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def test_winnerf_a_plus_recent_sales_are_matched() -> None:
    """위너프A+ must use the 위너프에이플러스 raw/mart key and have real recent sales."""
    payload = get_api("/api/market-status")
    card = next(card for card in payload["brand_cards"] if card["brand"] == "위너프A+")

    assert card["front"]["value_recent"] > 0
    assert card["front"]["ms_recent_pct"] > 0
    assert card["rank"] is not None

    brand = urllib.parse.quote("위너프A+")
    cause = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
    latest = cause["data"]["brand_ranking_stacked"]["yearly"][-1]
    target = next(row for row in latest["rankings"] if row["brand"] == "위너프A+")

    assert target["value"] > 0
    assert target["ms_pct"] > 0
    assert target["rank"] is not None


def test_zero_value_target_rows_do_not_get_synthetic_rank_one() -> None:
    """A catalog-only historical zero should not be displayed as market rank #1."""
    brand = urllib.parse.quote("위너프A+")
    payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")

    year_2021 = next(year for year in payload["data"]["brand_ranking_stacked"]["yearly"] if year["year"] == 2021)
    target = next(row for row in year_2021["rankings"] if row["brand"] == "위너프A+")
    actual_leader = max(
        (row for row in year_2021["rankings"] if row["brand"] != "위너프A+"),
        key=lambda row: row["value"],
    )

    assert target["value"] == 0
    assert target["ms_pct"] == 0
    assert target["rank"] is None
    assert actual_leader["rank"] == 1
    assert actual_leader["value"] > 0


def test_phase15_iqvia_q3q1_ratios_stay_preserved() -> None:
    """Phase 15 IQVIA overlap fix must survive catalog/ranking changes."""
    for brand_name in ("가드메트", "제이클", "뉴트로진", "페린젝트"):
        brand = urllib.parse.quote(brand_name)
        payload = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
        series = payload["data"]["sources_data"]["market_size_series"]
        q1 = next((row["value"] for row in series if row["period"] == "2025-Q1"), None)
        q3 = next((row["value"] for row in series if row["period"] == "2025-Q3"), None)

        if q1 is not None and q3 is not None:
            ratio = q3 / q1 * 100
            assert 70 <= ratio <= 130, f"{brand_name}: Q3/Q1 {ratio:.1f}%"
