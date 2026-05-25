from __future__ import annotations

from datetime import datetime


MKT_TEAM_MASTER = {
    "라베칸": "MKT 1팀",
    "라베칸듀오": "MKT 1팀",
    "제이클": "MKT 1팀",
    "가드렛": "MKT 1팀",
    "가드메트": "MKT 1팀",
    "타발리스": "MKT 1팀",
    "시그마트": "MKT 1팀",
    "리바로": "MKT 1팀",
    "리바로젯": "MKT 1팀",
    "리바로페노": "MKT 1팀",
    "리바로하이": "MKT 1팀",
    "리바로브이": "MKT 1팀",
    "트루패스": "MKT 1팀",
    "피나스타": "MKT 1팀",
    "제이다트": "MKT 1팀",
    "뉴트로진": "MKT 1팀",
    "모빌리아": "MKT 1팀",
    "악템라": "MKT 1팀",
    "페린젝트": "MKT 2팀",
    "베노훼럼": "MKT 2팀",
    "헴리브라": "MKT 2팀",
    "엔커버": "MKT 2팀",
    "위너프": "MKT 3팀",
    "위너프A+": "MKT 3팀",
    "플라주오피": "MKT 3팀",
}

BRAND_KEYS_V091 = {
    "brand",
    "market_id",
    "market_name",
    "market_name_short",
    "market_label_kor",
    "mkt_team",
    "sources",
    "atc_codes",
    "atc_desc",
    "is_jw",
    "is_target",
    "is_dual_source",
    "rank",
}


def test_brands_route_matches_v091_schema_and_mkt_team_master(client):
    response = client.get("/api/brands")
    assert response.status_code == 200
    brands = response.json()

    assert len(brands) == 25
    for brand in brands:
        assert set(brand) == BRAND_KEYS_V091, brand["brand"]
        assert brand["market_name_short"], brand["brand"]
        assert brand["market_label_kor"], brand["brand"]
        assert brand["atc_codes"], brand["brand"]
        assert brand["atc_desc"], brand["brand"]
        assert brand["mkt_team"] == MKT_TEAM_MASTER[brand["brand"]]


def test_market_status_uses_phase31_mkt_team_master(client):
    response = client.get("/api/market-status")
    assert response.status_code == 200

    cards = response.json()["brand_cards"]
    for card in cards:
        assert card["mkt_team"] == MKT_TEAM_MASTER[card["brand"]]


def test_deep_analysis_has_top_level_generated_at(client):
    response = client.get("/api/deep-analysis/가드메트")
    assert response.status_code == 200

    generated_at = response.json().get("generated_at")
    assert generated_at
    assert "T" in generated_at
    assert generated_at.endswith("+09:00")
    datetime.fromisoformat(generated_at)
