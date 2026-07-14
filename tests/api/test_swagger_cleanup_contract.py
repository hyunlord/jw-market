from __future__ import annotations

from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import brand_activity


@pytest.fixture(autouse=True)
def topic_period_bounds(monkeypatch) -> None:
    monkeypatch.setattr(
        brand_activity,
        "get_topic_period_bounds",
        lambda: {"available_start": "2024-06", "available_end": "2026-05"},
    )


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

    assert set(schemas["AtcFilter"]["properties"]) == {"atc4"}
    assert "UBIST" in schemas["AnalysisLevel"]["properties"]["ubist"]["description"]
    assert "판매사" in schemas["UbistAnalysisLevel"]["properties"]["seller"]["description"]
    assert "성분명" in schemas["IqviaAnalysisLevel"]["properties"]["molecule_desc"]["description"]
    assert "audit_code" in schemas["IqviaAnalysisLevel"]["properties"]
    assert "audit_code" in schemas["MarketFilter"]["properties"]["channel"]["description"]
    assert "visit_location" not in schemas["ChannelFilter"]["properties"]
    assert "specialty" not in schemas["ChannelFilter"]["properties"]
    assert "channel_axis" not in schemas["BrandActivityTopicsRequest"]["properties"]
    assert "channel_axis" not in schemas["BrandActivityInterestRxRequest"]["properties"]
    assert "channel_axis" not in schemas["CsdTimeseriesRequest"]["properties"]
    assert "channel_axis" not in schemas["MarketFilter"]["properties"]


def test_brand_activity_public_request_schema_is_iqvia_only() -> None:
    operation = app.openapi()["paths"]["/api/brand-activity/topics"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    payload = str(operation)

    assert "iqvia_nsa" in payload
    assert "filters.analysis_level.iqvia.audit_code" in payload
    assert "mfr_name_kor" in payload
    assert "filters.atc.atc4" in payload
    assert "Brand-Activity 처리 경로에서 사용하지 않습니다" not in payload

    schema_text = str(request_schema)
    assert "ubist" not in schema_text
    assert "seller" not in schema_text
    assert "pack_desc" in schema_text
    assert "mfr_name_kor" in schema_text
    assert "nhi_type" in schema_text
    assert "audit_code" in schema_text
    assert request_schema["properties"]["start_date"]["pattern"] == r"^\d{4}-(0[1-9]|1[0-2])$"
    assert request_schema["properties"]["end_date"]["pattern"] == r"^\d{4}-(0[1-9]|1[0-2])$"


def test_csd_activity_request_documents_nested_filter_fields() -> None:
    schema = app.openapi()["components"]["schemas"]["CsdActivitySeriesRequest"]

    assert schema["properties"]["filters"]["$ref"] == "#/components/schemas/MarketFilter"
    assert "strategic_cd" in schema["properties"]["view"]["description"]
    operation_text = str(app.openapi()["paths"]["/api/brand-activity/csd-activity-series"]["post"]["requestBody"])
    for field in ("atc4", "mfr_name_kor", "molecule_desc", "audit_code", "market_scope"):
        assert field in operation_text


def test_brand_activity_topics_response_documents_live_scope_and_brand_fields() -> None:
    response_schema = app.openapi()["paths"]["/api/brand-activity/topics"]["post"]["responses"]["200"]
    data_schema = response_schema["content"]["application/json"]["schema"]["properties"]["data"]["properties"]
    scope_fields = data_schema["scope"]["properties"]
    brand_fields = data_schema["brands"]["items"]["properties"]

    for field in (
        "applied_topic_filters",
        "filter_effect",
        "interest",
        "period_end",
        "period_start",
        "prescription_evolution",
        "sliced",
        "specialty",
        "top_n",
        "topic_set_version",
        "visit_location",
    ):
        assert field in scope_fields
    assert "sales_rank" in brand_fields
    period_fields = response_schema["content"]["application/json"]["schema"]["properties"]["meta"]["properties"]["period"]["properties"]
    assert set(period_fields) == {"start_date", "end_date", "available_start", "available_end"}


def test_dynamic_market_request_schema_exposes_only_public_filter_surface() -> None:
    schema = app.openapi()["paths"]["/api/dynamic-market"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    request_schema_text = str(schema)

    assert "channel_axis" not in request_schema_text
    assert "metrics" not in request_schema_text

    general_ubist = schema["oneOf"][0]["properties"]["filters"]["properties"]["analysis_level"]["properties"]["ubist"]["properties"]
    general_iqvia = schema["oneOf"][1]["properties"]["filters"]["properties"]["analysis_level"]["properties"]["iqvia"]["properties"]
    strategic_filters = schema["oneOf"][2]["properties"]["filters"]["properties"]

    assert "molecule" not in schema["oneOf"][0]["properties"]["filters"]["properties"]
    assert "molecule" in general_ubist
    assert {"facility", "specialty", "pairs"}.issubset(general_ubist)
    assert {"class", "strength_pack", "ox_gx"}.isdisjoint(general_ubist)
    assert {"mfr_name_kor", "molecule_type", "molecule_desc", "pack_desc", "strength", "nhi_type", "audit_code"}.issubset(general_iqvia)
    assert {"mfr", "nhi", "atc4"}.isdisjoint(general_iqvia)
    assert "atc4" in strategic_filters
    assert "analysis_level" not in strategic_filters
    for variant in schema["oneOf"]:
        options = variant["properties"]["options"]
        assert "" not in options
        assert set(options["properties"]) == {"period_range"}
        assert set(options["properties"]["period_range"]["properties"]) == {"start", "end"}
    assert "전략뷰도 top-level `filters.atc4`" in app.openapi()["paths"]["/api/dynamic-market"]["post"]["description"]
    assert "Swagger에서는" not in app.openapi()["paths"]["/api/dynamic-market"]["post"]["description"]


def test_dynamic_market_iqvia_example_keeps_pack_desc_with_peer_filters() -> None:
    operation = app.openapi()["paths"]["/api/dynamic-market"]["post"]
    examples = operation["requestBody"]["content"]["application/json"]["examples"]

    assert "general_iqvia_pack_desc_filter" not in examples
    iqvia_filters = examples["general_iqvia_filters"]["value"]["filters"]["analysis_level"]["iqvia"]
    assert {
        "mfr_name_kor",
        "molecule_type",
        "molecule_desc",
        "pack_desc",
        "strength",
        "nhi_type",
        "audit_code",
    }.issubset(iqvia_filters)
    assert "같은 `analysis_level.iqvia` 객체" in operation["description"]


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
    assert response.json() == {
        "data": expected,
        "meta": {
            "period": {
                "start_date": "2024-06",
                "end_date": "2026-05",
                "available_start": "2024-06",
                "available_end": "2026-05",
            },
            "request_normalized": True,
        },
    }
    assert captured["payload"]["filters"]["atc4"] == ["C10A1"]
    assert captured["payload"]["filter"]["analysis_level"]["ubist"]["seller"] == ["JW중외제약"]


def test_brand_activity_folds_analysis_level_audit_code_into_internal_slice(monkeypatch) -> None:
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
            "filters": {
                "atc": {"atc4": ["C10A1"]},
                "analysis_level": {"iqvia": {"audit_code": ["khpa"]}},
            },
        },
    )

    assert response.status_code == 200
    assert captured["payload"]["filters"]["atc4"] == ["C10A1"]
    assert captured["payload"]["filters"]["channel_axis"] == {"iqvia": {"audit_code": ["KHPA"]}}


def test_brand_activity_ignores_removed_nested_atc3_and_channel_shortcuts(monkeypatch) -> None:
    captured: dict[str, dict] = {}

    def fake_get_topic_brand_payload(payload: dict) -> dict:
        captured["payload"] = payload
        return {"brands": []}

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)
    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "visit_location": "의원",
            "specialty": "순환기내과",
            "filters": {
                "atc": {"atc3": ["C10A"], "atc4": ["C10A1"]},
                "channel": {"visit_location": ["병원"], "specialty": ["내과"]},
            },
        },
    )

    assert response.status_code == 200
    assert captured["payload"]["visit_location"] == "의원"
    assert captured["payload"]["specialty"] == "순환기내과"
    assert captured["payload"]["filters"]["atc4"] == ["C10A1"]
    assert "atc3" not in captured["payload"]["filters"]
    assert "visit_location" not in captured["payload"]["filters"]
    assert "specialty" not in captured["payload"]["filters"]


def test_brand_activity_accepts_bff_camel_case_payload(monkeypatch) -> None:
    # BFF compatibility aliases remain accepted but intentionally stay out of public OpenAPI.
    captured: dict[str, dict] = {}

    def fake_get_topic_brand_payload(payload: dict) -> dict:
        captured["payload"] = payload
        return {"brands": []}

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)

    response = TestClient(app).post(
        "/jw-brand-activity-mock/api/brand-activity/topics",
        json={
            "view": "general",
            "selectedBrand": "리바로",
            "topN": None,
            "filters": {
                "atc": {"atc3": None, "atc4": ["C10A1"]},
                "analysisLevel": {
                    "iqvia": {
                        "auditCode": ["khpa"],
                        "mfrNameKor": None,
                        "moleculeDesc": None,
                        "moleculeType": None,
                        "nhiType": None,
                        "packDesc": None,
                        "strength": None,
                    },
                    "ubist": None,
                },
                "channel": None,
            },
        },
    )

    assert response.status_code == 200
    assert captured["payload"]["selected_brand"] == "리바로"
    assert captured["payload"]["top_n"] == 5
    assert captured["payload"]["filters"]["atc4"] == ["C10A1"]
    assert captured["payload"]["filters"]["analysis_level"]["iqvia"]["audit_code"] == ["khpa"]
    assert captured["payload"]["filters"]["channel_axis"] == {"iqvia": {"audit_code": ["KHPA"]}}


def test_brand_activity_rejects_legacy_channel_axis_fields() -> None:
    client = TestClient(app)
    base_payload = {"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}}

    for path in (
        "/api/brand-activity/csd-timeseries",
        "/api/brand-activity/topics",
        "/api/brand-activity/interest-rx-matrix",
    ):
        top_level = {**base_payload, "channel_axis": {"iqvia": {"audit_code": ["KHPA"]}}}
        nested = {**base_payload, "filters": {"atc4": ["C10A1"], "channel_axis": {"iqvia": {"audit_code": ["KHPA"]}}}}

        assert client.post(path, json=top_level).status_code == 422
        assert client.post(path, json=nested).status_code == 422


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
    assert response.json() == {
        "data": expected,
        "meta": {
            "period": {
                "start_date": "2024-06",
                "end_date": "2026-05",
                "available_start": "2024-06",
                "available_end": "2026-05",
            }
        },
    }
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
    assert client.post("/jw-brand-activity-mock/api/brand-activity/csd-timeseries", json=base_payload).status_code == 200
    assert client.post("/jw-brand-activity-mock/api/brand-activity/topics", json=base_payload).status_code == 200
    assert client.post("/jw-brand-activity-mock/api/brand-activity/interest-rx-matrix", json=base_payload).status_code == 200


def test_reference_dynamic_sender_omits_removed_options() -> None:
    html = (Path(__file__).resolve().parents[2] / "docs/reference/jw_market_hardcoded_mockup_v3_4.html").read_text()

    assert "dynamic-top-n" not in html
    assert "top_n: Number" not in html
    assert "metrics: [" not in html



def test_filter_options_accepts_brand_and_alias_remains_callable(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_build_filter_options(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        payload: dict[str, object] = {
            "view": kwargs["view"],
            "source": kwargs["source"],
            "market_id": kwargs.get("market_id"),
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
    alias = client.get("/api/dynamic-market/brand-option-check?view=general&source=ubist&market_id=STALE&brand=리바로")

    assert no_brand.status_code == 200
    assert "brand_matched" not in no_brand.json()
    assert with_brand.json()["brand_matched"] == {"seller": ["JW중외제약"]}
    assert alias.status_code == 200
    assert alias.json()["brand_matched"] == {"seller": ["JW중외제약"]}
    assert [call.get("brand") for call in calls] == [None, "리바로", "리바로"]
    assert [call.get("market_id") for call in calls] == ["C10A1", "C10A1", None]
