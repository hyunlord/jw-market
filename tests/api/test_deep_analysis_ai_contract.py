from __future__ import annotations

from copy import deepcopy
import json

from fastapi.testclient import TestClient

from pipeline.scripts.api.routes import deep_analysis
from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes.deep_analysis import _normalize_ai_analysis_variants


def _analysis_payload(variant: str) -> dict:
    return {
        "analysis_variant": variant,
        "phenomenon": {"title": "현상", "body": "본문", "bullets": [], "evidence": []},
        "cause": {"title": "원인", "body": "본문", "bullets": [], "evidence": []},
        "prediction": {"title": "예측", "body": "본문", "bullets": [], "evidence": []},
        "recommendation": {"title": "권고", "body": "본문", "bullets": [], "evidence": []},
        "evidence_pool": [
            {"news_id": "n1", "title": "뉴스", "published_date": "2026-07-01", "score": 80},
            "kept-non-dict-entry",
        ],
    }


def test_normalize_ai_analysis_variants_removes_variant_and_published_date() -> None:
    # Given: short/long analysis variants still carry generation-only keys from cache.
    ai_analysis = _analysis_payload("base")
    data = {
        "ai_analysis": deepcopy(ai_analysis),
        "ai_analysis_short": _analysis_payload("short"),
        "ai_analysis_long": _analysis_payload("long"),
    }

    # When: the route normalizes serving-only variants.
    _normalize_ai_analysis_variants(data)

    # Then: short/long match the serving contract while ai_analysis remains untouched.
    assert data["ai_analysis"] == ai_analysis
    for key in ("ai_analysis_short", "ai_analysis_long"):
        payload = data[key]
        assert "analysis_variant" not in payload
        assert "published_date" not in payload["evidence_pool"][0]
        assert payload["evidence_pool"][1] == "kept-non-dict-entry"
        assert set(payload) == {
            "phenomenon",
            "cause",
            "prediction",
            "recommendation",
            "evidence_pool",
        }


def test_openapi_documents_deep_analysis_ai_variants_with_same_ref() -> None:
    # Given: the generated OpenAPI document for the deep-analysis route.
    app.openapi_schema = None
    schema = app.openapi()

    # When: the deep-analysis 200 response data schema is inspected.
    data_properties = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["data"]["properties"]

    # Then: base, short, and long analysis fields share the same documented payload contract.
    for key in ("ai_analysis", "ai_analysis_short", "ai_analysis_long"):
        assert data_properties[key]["oneOf"][0] == {"$ref": "#/components/schemas/AIAnalysis"}
        assert data_properties[key]["oneOf"][1] == {"$ref": "#/components/schemas/AIAnalysisUnavailable"}


def test_deep_analysis_route_normalizes_short_and_long_variants(monkeypatch) -> None:
    # Given: cache contains short/long generation fields but the separate ai_analysis row is already clean.
    cache_payload = {
        "data": {
            "ai_analysis_short": _analysis_payload("short"),
            "ai_analysis_long": _analysis_payload("long"),
        }
    }
    ai_analysis_payload = deepcopy(_analysis_payload("base"))
    ai_analysis_payload.pop("analysis_variant")
    ai_analysis_payload["evidence_pool"][0].pop("published_date")
    rows = iter(
        [
            {"response_json": json.dumps(cache_payload, ensure_ascii=False), "updated_at": "2026-07-07T09:00:00+09:00"},
            {"ai_analysis_json": json.dumps(ai_analysis_payload, ensure_ascii=False)},
        ]
    )
    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: next(rows))

    # When: the route serves a deep-analysis response.
    response = TestClient(app).get("/api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C")

    # Then: all AI analysis variants expose the same downstream-safe field shape.
    assert response.status_code == 200
    data = response.json()["data"]
    for key in ("ai_analysis", "ai_analysis_short", "ai_analysis_long"):
        payload = data[key]
        assert "analysis_variant" not in payload
        assert "published_date" not in payload["evidence_pool"][0]
