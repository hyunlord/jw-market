from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.main import app


def test_openapi_exposes_only_portal_shared_routes() -> None:
    schema = app.openapi()

    assert sorted(schema["paths"]) == [
        "/api/brand-activity/csd-activity-series",
        "/api/brand-activity/csd-timeseries",
        "/api/brand-activity/interest-rx-matrix",
        "/api/brand-activity/topics",
        "/api/brands",
        "/api/cause/{brand_name}",
        "/api/deep-analysis/{brand_name}",
        "/api/dynamic-market",
        "/api/dynamic-market/brand-option-check",
        "/api/dynamic-market/filter-options",
        "/api/health",
        "/api/market-filter/atc-options",
        "/api/market-status",
    ]


def test_openapi_hides_internal_legacy_and_experimental_routes() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    hidden_paths = {
        "/api/brand-activity/topics/{scope_id}",
        "/api/market-scope/cause",
        "/api/market-scope/options",
        "/api/market-scope/resolve",
    }

    assert hidden_paths.isdisjoint(paths)


def test_shared_dynamic_routes_document_without_response_model_trimming() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    cause = schema["paths"]["/api/cause/{brand_name}"]["get"]
    dynamic = schema["paths"]["/api/dynamic-market"]["post"]
    filter_options = schema["paths"]["/api/dynamic-market/filter-options"]["get"]
    brand_option_check = schema["paths"]["/api/dynamic-market/brand-option-check"]["get"]

    assert cause["tags"] == ["Portal-Core"]
    assert dynamic["tags"] == ["Dynamic-Market"]
    assert filter_options["tags"] == ["Dynamic-Market"]
    assert brand_option_check["tags"] == ["Dynamic-Market"]
    assert "markets" in str(cause["responses"]["200"])
    assert "23섹션" in str(dynamic["responses"]["200"])
    assert "brand_matched" in str(filter_options["responses"]["200"])
    assert "brand_matched" in str(brand_option_check["responses"]["200"])

    for route in app.routes:
        path = getattr(route, "path", "")
        if path in {"/api/cause/{brand_name}", "/api/dynamic-market", "/api/dynamic-market/filter-options", "/api/dynamic-market/brand-option-check"}:
            assert getattr(route, "response_model", None) is None


def test_brand_option_check_openapi_removes_public_market_id_parameter() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    parameters = schema["paths"]["/api/dynamic-market/brand-option-check"]["get"]["parameters"]

    assert {parameter["name"] for parameter in parameters} == {"brand", "view", "source"}


def test_brand_activity_csd_routes_are_portal_shared_docs_only() -> None:
    schema = app.openapi()

    topics = schema["paths"]["/api/brand-activity/topics"]["post"]
    timeseries = schema["paths"]["/api/brand-activity/csd-timeseries"]["post"]
    activity_series = schema["paths"]["/api/brand-activity/csd-activity-series"]["post"]
    matrix = schema["paths"]["/api/brand-activity/interest-rx-matrix"]["post"]

    assert topics["tags"] == ["Brand-Activity"]
    assert timeseries["tags"] == ["Brand-Activity"]
    assert activity_series["tags"] == ["Brand-Activity"]
    assert matrix["tags"] == ["Brand-Activity"]
    assert "브랜드별 토픽 그리드" in topics["summary"]
    assert "region=TOTAL" in timeseries["description"]
    assert "CSD 활동량" in activity_series["summary"]
    assert "interest×처방빈도" in matrix["summary"]
    assert "topic_shares" in str(topics["responses"]["200"])
    assert "market_totals" in str(timeseries["responses"]["200"])
    assert "market_average" in str(matrix["responses"]["200"])
    assert "channel_axis" not in str(topics["requestBody"])
    assert "analysis_level" in str(timeseries["requestBody"])

    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        if "POST" in methods and path in {
            "/api/brand-activity/topics",
            "/api/brand-activity/csd-timeseries",
            "/api/brand-activity/csd-activity-series",
            "/api/brand-activity/interest-rx-matrix",
        }:
            assert getattr(route, "response_model", None) is None


def test_interest_rx_matrix_documents_prescription_evolution_y_axis() -> None:
    schema = app.openapi()
    operation = schema["paths"]["/api/brand-activity/interest-rx-matrix"]["post"]
    response_description = operation["responses"]["200"]["description"]

    assert "Y축은 prescription_evolution_score" in operation["description"]
    assert "Y=prescription_evolution_score" in response_description
    assert "Y축은 interest_score" not in operation["description"]
    assert "Y=interest_score" not in response_description


def test_dynamic_market_documents_competitive_dynamics_contract() -> None:
    schema = app.openapi()

    operation = schema["paths"]["/api/dynamic-market"]["post"]
    payload = str(operation)

    assert "competitive_dynamics" in payload
    assert "cd_market_id 오름차순 첫 번째" in payload
    assert "cd_market_id(경쟁구도 ID)" not in payload


def test_dynamic_market_documents_field_semantics_and_source_filters() -> None:
    schema = app.openapi()

    operation = schema["paths"]["/api/dynamic-market"]["post"]
    payload = str(operation)

    assert "missing 처리" in payload
    assert "null 처리" in payload
    assert "mfr_name_kor" in payload
    assert "molecule_strength" in payload
    assert "PACK DESC" in payload
    assert "general_iqvia_pack_desc_filter" not in payload
    assert "pack_desc/strength/nhi_type" in payload
    assert "analysis_level dimension is disabled for dynamic filters: pack_desc" not in payload
    assert "analysis_level must match selected source" in payload
    assert "filters.atc4" in payload
    assert set(operation["requestBody"]["content"]["application/json"]["examples"]) >= {
        "general_baseline",
        "general_ubist_filters",
        "general_iqvia_filters",
        "market_landscape",
        "competitive_dynamics",
    }


def test_brand_activity_documents_shared_filter_differences() -> None:
    schema = app.openapi()

    topics = schema["paths"]["/api/brand-activity/topics"]["post"]
    timeseries = schema["paths"]["/api/brand-activity/csd-timeseries"]["post"]
    matrix = schema["paths"]["/api/brand-activity/interest-rx-matrix"]["post"]

    for operation in (topics, timeseries, matrix):
        payload = str(operation)
        assert "Dynamic-Market과 같은 시장 필터 개념" in payload
        assert "filters.atc4" in payload
        assert "legacy" in payload
        assert "unknown field" in payload


def test_market_filter_atc_options_keeps_existing_response_model_and_docs() -> None:
    schema = app.openapi()

    operation = schema["paths"]["/api/market-filter/atc-options"]["get"]

    assert operation["tags"] == ["Dynamic-Market"]
    assert operation["summary"] == "시장필터 1단계 ATC 옵션"
    assert "flag=true" in operation["description"]
    assert "MarketFilterAtcOptionsResponse" in str(operation)


def test_deep_analysis_documents_base_short_and_long_ai_analysis_with_same_schema() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    data_properties = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["data"]["properties"]

    assert data_properties["ai_analysis"] == data_properties["ai_analysis_short"]
    assert data_properties["ai_analysis"] == data_properties["ai_analysis_long"]
    assert data_properties["ai_analysis"]["oneOf"] == [
        {"$ref": "#/components/schemas/AIAnalysis"},
        {"$ref": "#/components/schemas/AIAnalysisUnavailable"},
    ]
    assert "AIAnalysisStage" in schema["components"]["schemas"]


def test_deep_analysis_openapi_keeps_existing_portal_docs() -> None:
    schema = app.openapi()

    assert "23섹션" in str(schema["paths"]["/api/dynamic-market"]["post"]["responses"]["200"])
    assert "topic_shares" in str(schema["paths"]["/api/brand-activity/topics"]["post"]["responses"]["200"])
    assert "brand_matched" in str(schema["paths"]["/api/dynamic-market/filter-options"]["get"]["responses"]["200"])
