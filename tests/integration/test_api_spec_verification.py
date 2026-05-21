from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_FILE = REPO_ROOT / "docs/reference/JW_Market_Analysis_API_Spec_20260520.html"
ORIGINAL_MOCKUP = REPO_ROOT / "docs/reference/jw_market_hardcoded_mockup_20260520.html"
V2_MOCKUP = REPO_ROOT / "docs/reference/jw_market_hardcoded_mockup_v2.html"

API_BASE = os.getenv("LOCAL_API_BASE", "http://localhost:8000").rstrip("/")
ARTIFACT_DIR = Path(os.getenv("SPEC_ARTIFACT_DIR", "/tmp/jw_spec_verification"))

run_live_backend = pytest.mark.skipif(
    os.getenv("RUN_API_SPEC_VERIFICATION") != "1",
    reason="set RUN_API_SPEC_VERIFICATION=1 with a local backend on LOCAL_API_BASE",
)


CURRENT_CONTRACT: dict[str, Any] = {
    "endpoints": [
        {
            "name": "health",
            "method": "GET",
            "path": "/api/health",
            "required_response_keys": ["status"],
        },
        {
            "name": "brands",
            "method": "GET",
            "path": "/api/brands",
            "query": {
                "view": ["general", "strategic_ml", "strategic_cd"],
                "source": ["ubist", "iqvia"],
            },
            "required_response_keys": ["brands"],
            "required_item_keys": ["brand_key", "brand_name", "is_jw"],
        },
        {
            "name": "market-status",
            "method": "GET",
            "path": "/api/market-status/{market_id}",
            "required_query": ["view", "source", "measure"],
            "required_response_keys": [
                "market_id",
                "market_name",
                "view",
                "source",
                "measure",
                "market_size_series",
                "hhi_series_5y",
            ],
        },
        {
            "name": "cause",
            "method": "GET",
            "path": "/api/cause/{brand}",
            "required_query": ["view", "source", "measure"],
            "required_response_keys": ["brand_name", "brand_key", "view", "source", "measure", "data"],
        },
        {
            "name": "deep-analysis",
            "method": "GET",
            "path": "/api/deep-analysis/{brand}",
            "required_query": ["view", "source", "measure"],
            "required_response_keys": ["brand", "brand_key", "view", "source", "measure"],
        },
    ]
}


def _write_json_artifact(name: str, payload: Any) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _get(path: str, params: dict[str, str] | None = None, timeout: float = 30.0) -> httpx.Response:
    return httpx.get(f"{API_BASE}{path}", params=params or {}, timeout=timeout)


def _assert_keys(body: dict[str, Any], keys: list[str], endpoint: str) -> None:
    missing = [key for key in keys if key not in body]
    assert not missing, f"{endpoint} missing response keys: {missing}; actual={sorted(body)}"


def test_original_mockup_remains_unmodified() -> None:
    html = ORIGINAL_MOCKUP.read_text(encoding="utf-8")
    assert "const API_BASE = 'MOCK';" in html
    assert "docs/reference/jw_market_hardcoded_mockup_v2.html" not in html


def test_reference_spec_is_present_and_documents_legacy_contract() -> None:
    html = SPEC_FILE.read_text(encoding="utf-8")
    assert "/api/market-status" in html
    assert "market_landscape" in html
    assert "competitive_dynamics" in html
    assert "UBIST" in html


def test_mockup_v2_uses_current_api_contract() -> None:
    assert V2_MOCKUP.exists(), "create a forked mockup v2 instead of editing the original"
    html = V2_MOCKUP.read_text(encoding="utf-8")

    assert "const API_BASE = 'http://localhost:8000';" in html
    assert "const API_BASE = 'MOCK';" not in html
    assert "/api/market-status/${" in html or "/api/market-status/`" in html
    assert "const DEFAULT_VIEW = 'strategic_ml';" in html
    assert "const DEFAULT_SOURCE = 'ubist';" in html
    assert "view=${DEFAULT_VIEW}" in html
    assert "source=${DEFAULT_SOURCE}" in html or "source=${source}" in html
    assert "view=market_landscape" not in html
    assert "view=competitive_dynamics" not in html
    assert "source=UBIST" not in html
    assert "source=IQVIA" not in html


@run_live_backend
def test_health_spec() -> None:
    response = _get("/api/health", timeout=10.0)
    assert response.status_code == 200
    body = response.json()
    _assert_keys(body, ["status"], "/api/health")
    assert isinstance(body["status"], str)


@run_live_backend
@pytest.mark.parametrize(
    ("view", "source"),
    [
        (None, None),
        ("general", None),
        ("strategic_ml", None),
        ("strategic_cd", None),
        (None, "ubist"),
        (None, "iqvia"),
        ("strategic_ml", "ubist"),
    ],
)
def test_brands_spec(view: str | None, source: str | None) -> None:
    params = {}
    if view is not None:
        params["view"] = view
    if source is not None:
        params["source"] = source

    response = _get("/api/brands", params=params)
    assert response.status_code == 200, f"{params}: {response.status_code} {response.text[:300]}"
    body = response.json()
    _assert_keys(body, ["brands"], "/api/brands")
    assert isinstance(body["brands"], list)

    if body["brands"]:
        brand = body["brands"][0]
        _assert_keys(brand, ["brand_key", "brand_name", "is_jw"], "/api/brands item")
        assert isinstance(brand["brand_key"], str)
        assert isinstance(brand["brand_name"], str)
        assert isinstance(brand["is_jw"], bool)


@run_live_backend
@pytest.mark.parametrize(
    ("market_id", "view", "source", "measure"),
    [
        ("ml_006", "strategic_ml", "ubist", "sales"),
        ("ml_006", "strategic_ml", "ubist", "volume"),
        ("ml_006", "strategic_ml", "iqvia", "sales"),
        ("ml_006", "strategic_ml", "iqvia", "unit"),
    ],
)
def test_market_status_spec(market_id: str, view: str, source: str, measure: str) -> None:
    params = {"view": view, "source": source, "measure": measure}
    response = _get(f"/api/market-status/{market_id}", params=params)
    assert response.status_code == 200, f"{market_id}/{params}: {response.status_code} {response.text[:300]}"
    body = response.json()
    _assert_keys(
        body,
        ["market_id", "market_name", "view", "source", "measure", "market_size_series", "hhi_series_5y"],
        "/api/market-status",
    )
    assert body["market_id"] == market_id
    assert body["view"] == view
    assert body["source"] in {source, "iqvia_nsa"}
    assert body["measure"] == measure
    assert isinstance(body["market_size_series"], dict)


@run_live_backend
@pytest.mark.parametrize(
    ("brand", "view", "source", "measure"),
    [
        ("리바로", "strategic_ml", "ubist", "sales"),
        ("가드메트", "general", "iqvia", "sales"),
        ("페린젝트", "strategic_ml", "iqvia", "sales"),
    ],
)
def test_cause_spec(brand: str, view: str, source: str, measure: str) -> None:
    params = {"view": view, "source": source, "measure": measure}
    response = _get(f"/api/cause/{brand}", params=params)
    assert response.status_code in (200, 404), f"{brand}/{params}: {response.status_code} {response.text[:300]}"
    if response.status_code == 404:
        return

    body = response.json()
    _assert_keys(body, ["brand_name", "brand_key", "view", "source", "measure", "data"], "/api/cause")
    assert body["view"] == view
    assert body["source"] in {source, "iqvia_nsa"}
    assert body["measure"] == measure
    assert isinstance(body["data"], dict)
    assert "kpi" in body["data"]


@run_live_backend
@pytest.mark.parametrize(
    ("brand", "view", "source", "measure"),
    [
        ("리바로", "strategic_ml", "ubist", "sales"),
        ("가드메트", "general", "iqvia", "sales"),
    ],
)
def test_deep_analysis_spec(brand: str, view: str, source: str, measure: str) -> None:
    params = {"view": view, "source": source, "measure": measure}
    response = _get(f"/api/deep-analysis/{brand}", params=params)
    assert response.status_code in (200, 404), f"{brand}/{params}: {response.status_code} {response.text[:300]}"
    if response.status_code == 404:
        return

    body = response.json()
    _assert_keys(body, ["brand", "brand_key", "view", "source", "measure"], "/api/deep-analysis")
    assert body["view"] == view
    assert body["source"] in {source, "iqvia_nsa"}
    assert body["measure"] == measure


@run_live_backend
def test_legacy_market_status_no_params_returns_422() -> None:
    response = _get("/api/market-status", timeout=10.0)
    assert response.status_code == 422


@run_live_backend
def test_legacy_view_market_landscape_returns_422() -> None:
    response = _get(
        "/api/cause/가드메트",
        params={"view": "market_landscape", "source": "UBIST", "measure": "sales"},
        timeout=10.0,
    )
    assert response.status_code == 422


@run_live_backend
def test_legacy_deep_analysis_no_params_returns_422() -> None:
    response = _get("/api/deep-analysis/가드메트", timeout=10.0)
    assert response.status_code == 422


@run_live_backend
def test_dump_full_response_for_each_endpoint() -> None:
    samples = [
        ("/api/health", {}),
        ("/api/brands", {"view": "strategic_ml", "source": "ubist"}),
        ("/api/market-status/ml_006", {"view": "strategic_ml", "source": "ubist", "measure": "sales"}),
        ("/api/cause/리바로", {"view": "strategic_ml", "source": "ubist", "measure": "sales"}),
        ("/api/deep-analysis/리바로", {"view": "strategic_ml", "source": "ubist", "measure": "sales"}),
    ]

    dumps: dict[str, Any] = {}
    for path, params in samples:
        response = _get(path, params=params)
        key = f"{path}?{params}" if params else path
        dumps[key] = {
            "status_code": response.status_code,
            "params": params,
            "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
        }

    _write_json_artifact("full_responses.json", dumps)
    _write_json_artifact("01_spec_contract.json", CURRENT_CONTRACT)
