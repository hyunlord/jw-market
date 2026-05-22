from __future__ import annotations


CANONICAL_25 = {
    "라베칸",
    "라베칸듀오",
    "제이클",
    "가드렛",
    "가드메트",
    "타발리스",
    "시그마트",
    "리바로",
    "리바로젯",
    "리바로페노",
    "리바로하이",
    "리바로브이",
    "트루패스",
    "피나스타",
    "제이다트",
    "뉴트로진",
    "모빌리아",
    "악템라",
    "페린젝트",
    "베노훼럼",
    "헴리브라",
    "위너프",
    "위너프A+",
    "엔커버",
    "플라주오피",
}


def test_brands_returns_25_canonical(client):
    response = client.get("/api/brands")
    assert response.status_code == 200
    brands = response.json()
    assert len(brands) == 25
    assert {brand["brand"] for brand in brands} == CANONICAL_25


def test_brands_filters_by_q_and_market_id(client):
    by_q = client.get("/api/brands?q=리바")
    assert by_q.status_code == 200
    assert {brand["brand"] for brand in by_q.json()} == {"리바로", "리바로젯", "리바로페노", "리바로하이", "리바로브이"}

    by_market = client.get("/api/brands?market_id=strategy_006")
    assert by_market.status_code == 200
    assert {brand["brand"] for brand in by_market.json()} == {"리바로", "리바로젯"}


def test_market_status_has_separated_kpi_and_25_cards(client):
    response = client.get("/api/market-status")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["kpi"]) == {"ubist", "iqvia"}
    assert len(payload["brand_cards"]) == 25


def test_cause_full_query_returns_spec_payload(client):
    response = client.get("/api/cause/리바로?view=market_landscape&source=UBIST&measure=sales")
    assert response.status_code == 200
    payload = response.json()
    assert payload["brand"] == "리바로"
    assert payload["market_id"] == "strategy_006"
    assert payload["view"] == "market_landscape"
    assert payload["source"] == "UBIST"
    assert payload["measure"] == "sales"
    assert payload["data"] is not None


def test_cause_brand_not_in_source_returns_null_data(client):
    response = client.get("/api/cause/리바로?view=market_landscape&source=IQVIA&measure=sales")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] is None
    assert payload["reason"] == "brand_not_in_source"


def test_deep_analysis_no_query_and_noncanonical_brand(client):
    canonical = client.get("/api/deep-analysis/리바로")
    assert canonical.status_code == 200
    assert "UBIST.sales" in canonical.json()["data"]["forecast"]["by_combo"]

    market_member = client.get("/api/deep-analysis/나도가드")
    assert market_member.status_code == 200
    assert market_member.json()["brand"] == "나도가드"


def test_deep_analysis_not_found(client):
    response = client.get("/api/deep-analysis/존재안하는브랜드xyz")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "brand_not_found"


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["brands_loaded"] == 25
    assert isinstance(payload["markets_loaded"], int)
    assert payload["version"] == "0.10.0"
