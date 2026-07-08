from __future__ import annotations

from pathlib import Path
import sys

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import brand_activity


def test_openapi_hides_internal_and_alias_routes() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    assert "/api/health" in paths
    assert "/api/market-scope/options" not in paths
    assert "/api/market-scope/resolve" not in paths
    assert "/api/market-scope/cause" not in paths
    assert set(paths["/api/brand-activity/topics"]) == {"post"}
    assert "/api/brand-activity/topics/{scope_id}" not in paths

    assert "/api/brands" in paths
    assert "/api/dynamic-market" in paths
    assert "/api/dynamic-market/filter-options" in paths
    assert "/api/dynamic-market/brand-option-check" in paths
    assert "/api/brand-activity/csd-timeseries" in paths
    assert "/api/brand-activity/interest-rx-matrix" in paths


def test_brand_activity_filter_schema_exposes_nested_descriptions() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert schemas["AtcFilter"]["properties"]["atc3"]["description"].startswith("ATC3 코드")
    assert "UBIST" in schemas["AnalysisLevel"]["properties"]["ubist"]["description"]
    assert "판매사" in schemas["UbistAnalysisLevel"]["properties"]["seller"]["description"]
    assert "성분명" in schemas["IqviaAnalysisLevel"]["properties"]["molecule_desc"]["description"]
    assert "채널 필터" in schemas["MarketFilter"]["properties"]["channel"]["description"]
    assert "채널 축" in schemas["BrandActivityTopicsRequest"]["properties"]["channel_axis"]["description"]
    assert "channel_axis" in schemas["MarketFilter"]["properties"]


def test_dynamic_market_request_schema_exposes_only_public_filter_surface() -> None:
    schema = app.openapi()["paths"]["/api/dynamic-market"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    request_schema_text = str(schema)

    assert "channel_axis" not in request_schema_text
    assert "metrics" not in request_schema_text

    general_ubist = schema["oneOf"][0]["properties"]["filters"]["properties"]["analysis_level"]["properties"]["ubist"]["properties"]
    general_iqvia = schema["oneOf"][1]["properties"]["filters"]["properties"]["analysis_level"]["properties"]["iqvia"]["properties"]
    strategic = schema["oneOf"][2]["properties"]["filters"]["properties"]["analysis_level"]["properties"]

    assert "molecule" not in schema["oneOf"][0]["properties"]["filters"]["properties"]
    assert {"facility", "specialty", "pairs"}.issubset(general_ubist)
    assert {"class", "molecule", "strength_pack", "ox_gx"}.isdisjoint(general_ubist)
    assert {"mfr_name_kor", "molecule_type", "molecule_desc", "pack_desc", "strength", "nhi_type", "audit_code"}.issubset(general_iqvia)
    assert {"mfr", "nhi"}.isdisjoint(general_iqvia)
    assert {"class", "molecule", "strength_pack", "ox_gx", "atc3"}.issubset(strategic["ubist"]["properties"])
    assert {"mfr", "nhi"}.issubset(strategic["iqvia"]["properties"])
    assert "audit_code" not in strategic["iqvia"]["properties"]


def test_brand_activity_accepts_nested_filters_and_legacy_flat_filter(monkeypatch) -> None:
    captured: dict[str, dict] = {}
    expected = {"scope": {"view": "general"}, "brands": []}

    def fake_get_topic_brand_payload(payload: dict) -> dict:
        captured["payload"] = payload
        return expected

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc": {"atc4": ["C10A1"]}, "analysis_level": {"ubist": {"seller": ["JW중외제약"]}}},
            "filter": {"atc4": ["legacy"]},
            "unknown_field": "ignored",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"data": expected}
    assert captured["payload"]["filters"]["atc"]["atc4"] == ["C10A1"]
    assert captured["payload"]["filter"]["analysis_level"]["ubist"]["seller"] == ["JW중외제약"]


def test_brand_activity_preserves_flat_filter_payload(monkeypatch) -> None:
    captured: dict[str, dict] = {}
    expected = {"scope": {"view": "general"}, "brands": []}

    def fake_get_topic_brand_payload(payload: dict) -> dict:
        captured["payload"] = payload
        return expected

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}},
    )

    assert response.status_code == 200
    assert response.json() == {"data": expected}
    assert captured["payload"]["filters"]["atc4"] == ["C10A1"]
    assert captured["payload"]["filter"]["atc4"] == ["C10A1"]


def test_brand_activity_post_routes_still_return_200(monkeypatch) -> None:
    monkeypatch.setattr(brand_activity, "get_csd_timeseries", lambda _payload: {"series": []})
    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", lambda _payload: {"brands": []})
    monkeypatch.setattr(brand_activity, "get_interest_rx_matrix", lambda _payload: {"brands": []})

    client = TestClient(app)
    base_payload = {"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}}

    assert client.post("/api/brand-activity/csd-timeseries", json=base_payload).status_code == 200
    assert client.post("/api/brand-activity/topics", json=base_payload).status_code == 200
    assert client.post("/api/brand-activity/interest-rx-matrix", json=base_payload).status_code == 200


def test_filter_options_accepts_brand_and_alias_remains_callable(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_build_filter_options(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        payload: dict[str, object] = {
            "view": kwargs["view"],
            "source": kwargs["source"],
            "market_id": kwargs["market_id"],
            "dimensions": [],
            "atc": {"selectable_levels": ["atc3", "atc4"]},
        }
        if kwargs.get("brand"):
            payload["brand"] = kwargs["brand"]
            payload["brand_matched"] = {"seller": ["JW중외제약"]}
        return payload

    monkeypatch.setattr("pipeline.scripts.api.routes.dynamic_market.build_filter_options", fake_build_filter_options)

    client = TestClient(app)
    no_brand = client.get("/api/dynamic-market/filter-options?view=general&source=ubist&market_id=C10A1")
    with_brand = client.get("/api/dynamic-market/filter-options?view=general&source=ubist&market_id=C10A1&brand=리바로")
    alias = client.get("/api/dynamic-market/brand-option-check?view=general&source=ubist&market_id=C10A1&brand=리바로")

    assert no_brand.status_code == 200
    assert "brand_matched" not in no_brand.json()
    assert with_brand.json()["brand_matched"] == {"seller": ["JW중외제약"]}
    assert alias.status_code == 200
    assert alias.json()["brand_matched"] == {"seller": ["JW중외제약"]}
    assert [call.get("brand") for call in calls] == [None, "리바로", "리바로"]
