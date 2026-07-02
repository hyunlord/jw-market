from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.main import app


def test_openapi_exposes_only_portal_shared_routes() -> None:
    schema = app.openapi()

    assert sorted(schema["paths"]) == [
        "/api/brands",
        "/api/cause/{brand_name}",
        "/api/deep-analysis/{brand_name}",
        "/api/dynamic-market",
        "/api/dynamic-market/filter-options",
        "/api/health",
        "/api/market-filter/atc-options",
        "/api/market-status",
    ]


def test_openapi_hides_internal_legacy_and_experimental_routes() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    hidden_paths = {
        "/api/brand-activity/csd-timeseries",
        "/api/brand-activity/interest-rx-matrix",
        "/api/brand-activity/topics",
        "/api/brand-activity/topics/{scope_id}",
        "/api/dynamic-market/brand-option-check",
        "/api/market-scope/cause",
        "/api/market-scope/options",
        "/api/market-scope/resolve",
    }

    assert hidden_paths.isdisjoint(paths)


def test_shared_dynamic_routes_document_without_response_model_trimming() -> None:
    schema = app.openapi()

    cause = schema["paths"]["/api/cause/{brand_name}"]["get"]
    dynamic = schema["paths"]["/api/dynamic-market"]["post"]
    filter_options = schema["paths"]["/api/dynamic-market/filter-options"]["get"]

    assert cause["tags"] == ["Portal-Core"]
    assert dynamic["tags"] == ["Dynamic-Market"]
    assert filter_options["tags"] == ["Dynamic-Market"]
    assert "markets" in str(cause["responses"]["200"])
    assert "23섹션" in str(dynamic["responses"]["200"])
    assert "brand_matched" in str(filter_options["responses"]["200"])

    for route in app.routes:
        path = getattr(route, "path", "")
        if path in {"/api/cause/{brand_name}", "/api/dynamic-market", "/api/dynamic-market/filter-options"}:
            assert getattr(route, "response_model", None) is None


def test_market_filter_atc_options_keeps_existing_response_model_and_docs() -> None:
    schema = app.openapi()

    operation = schema["paths"]["/api/market-filter/atc-options"]["get"]

    assert operation["tags"] == ["Dynamic-Market"]
    assert operation["summary"] == "시장필터 1단계 ATC 옵션"
    assert "flag=true" in operation["description"]
    assert "MarketFilterAtcOptionsResponse" in str(operation)
