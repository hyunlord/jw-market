from __future__ import annotations


def test_multi_market_brand_uses_first_market_and_lists_all_markets(client):
    response = client.get("/api/cause/나도가드?view=market_landscape&source=UBIST&measure=sales")
    assert response.status_code == 200
    payload = response.json()
    assert payload["brand"] == "나도가드"
    assert payload["market_id"] == "strategy_005"
    assert [market["market_id"] for market in payload["markets"]] == ["strategy_005", "strategy_008"]
    assert payload["markets"][0]["is_primary"] is True


def test_competitive_dynamics_keeps_cd_branch_payloads_separate(client):
    high = client.get("/api/cause/리바로하이?view=competitive_dynamics&source=UBIST&measure=sales")
    v = client.get("/api/cause/리바로브이?view=competitive_dynamics&source=UBIST&measure=sales")
    assert high.status_code == 200
    assert v.status_code == 200
    assert high.json()["market_id"] == "strategy_008"
    assert v.json()["market_id"] == "strategy_008"
    assert high.json()["data"]["kpi"]["market_size_recent"] != v.json()["data"]["kpi"]["market_size_recent"]
