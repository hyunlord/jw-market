from __future__ import annotations

import json

from fastapi.testclient import TestClient

from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import deep_analysis


def test_deep_analysis_route_serves_brand_factors(monkeypatch) -> None:
    rows = iter(
        [
            {
                "response_json": json.dumps({"data": {"forecast": {"by_combo": {}}}}, ensure_ascii=False),
                "brand_factors": json.dumps({"atc": ["C10A1"], "ubist": {"seller": ["JW중외제약"]}, "iqvia": {}}, ensure_ascii=False),
                "updated_at": "2026-07-09T09:00:00+09:00",
            },
            {"ai_analysis_json": "{}"},
        ]
    )
    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: next(rows))

    response = TestClient(app).get("/api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C")

    assert response.status_code == 200
    assert response.json()["data"]["brand_factors"]["atc"] == ["C10A1"]
    assert response.json()["data"]["brand_factors"]["ubist"]["seller"] == ["JW중외제약"]


def test_deep_analysis_openapi_documents_brand_factors() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    data_schema = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["data"]

    assert "brand_factors" in data_schema["properties"]
    assert "pack_desc" in data_schema["properties"]["brand_factors"]["properties"]["iqvia"]["properties"]
