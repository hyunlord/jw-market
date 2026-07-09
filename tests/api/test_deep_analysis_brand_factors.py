from __future__ import annotations

import json

from fastapi.testclient import TestClient

from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice
from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import deep_analysis


def test_deep_analysis_route_serves_source_scoped_brand_factors(monkeypatch) -> None:
    rows = iter(
        [
            {
                "response_json": json.dumps({"data": {"forecast": {"by_combo": {}}}}, ensure_ascii=False),
                "brand": "리바로",
                "brand_key": "리바로",
                "atc4_code": "C10A1",
                "brand_factors": json.dumps({"atc": ["C10A1"], "ubist": {"seller": ["JW중외제약"]}, "iqvia": {}}, ensure_ascii=False),
                "updated_at": "2026-07-09T09:00:00+09:00",
            },
            {"ai_analysis_json": "{}"},
            {"ai_analysis_short_json": None, "ai_analysis_long_json": None},
            [
                {
                    "brand_key": "크레스토",
                    "brand_name": "크레스토",
                    "factors_json": json.dumps({"atc": ["C10A1"], "ubist": {}, "iqvia": {"mfr_name_kor": ["AZ"]}}, ensure_ascii=False),
                    "strength_json": json.dumps({"available": False, "reason": "not_generated"}, ensure_ascii=False),
                    "strength_generated_at": None,
                    "strength_workflow_rev": None,
                    "updated_at": "2026-07-09T09:00:00+09:00",
                }
            ],
            [
                {
                    "brand_key": "리바로",
                    "serving_brand_name": "리바로",
                    "source": "iqvia",
                    "strength_summary_json": json.dumps(
                        {"profile_display": {"headline": "iqvia"}, "strength_items": ["IQVIA 강점"], "limitations": []},
                        ensure_ascii=False,
                    ),
                }
            ],
            [],
        ]
    )
    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: next(rows))
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: next(rows))
    monkeypatch.setattr(
        deep_analysis,
        "resolve_brand_set",
        lambda **_kwargs: type(
            "Resolution",
            (),
            {
                "choices": (
                    BrandChoice("리바로", "리바로", 3, True),
                    BrandChoice("크레스토", "크레스토", 1, False),
                    BrandChoice("리피토", "리피토", 2, False),
                    BrandChoice("로수바미브", "로수바미브", 4, False),
                    BrandChoice("아토젯", "아토젯", 5, False),
                    BrandChoice("바이토린", "바이토린", 6, False),
                )
            },
        )(),
    )

    response = TestClient(app).get("/api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C")

    assert response.status_code == 200
    data = response.json()["data"]
    assert "brand_elements" not in data
    assert "brand_strength" not in data
    assert "strength_by_source" not in data
    assert [item["role"] for item in data["brand_factors"]] == ["selected", *["competitor"] * 5]
    assert [item["brand_key"] for item in data["brand_factors"]] == [
        "리바로",
        "크레스토",
        "리피토",
        "로수바미브",
        "아토젯",
        "바이토린",
    ]
    selected = data["brand_factors"][0]
    assert selected["ubist"]["factors"]["available"] is True
    assert selected["ubist"]["factors"]["reason"] is None
    assert selected["ubist"]["factors"]["values"]["seller"] == ["JW중외제약"]
    assert selected["ubist"]["strength"] == {}
    assert selected["iqvia"]["factors"]["available"] is False
    assert selected["iqvia"]["strength"]["strength_items"] == ["IQVIA 강점"]
    assert set(selected) == {"brand", "brand_key", "role", "rank", "iqvia", "ubist"}
    competitor = data["brand_factors"][1]
    assert competitor["iqvia"]["factors"]["available"] is True
    assert competitor["iqvia"]["factors"]["values"]["mfr_name_kor"] == ["AZ"]
    assert competitor["iqvia"]["strength"] == {}
    assert competitor["ubist"] == {}
    assert data["brand_factors"][2]["iqvia"] == {}
    assert data["brand_factors"][2]["ubist"] == {}
    serialized = json.dumps(data, ensure_ascii=False)
    assert "brand_elements" not in serialized
    assert "strength_by_source" not in serialized


def test_deep_analysis_openapi_documents_source_scoped_brand_factors() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    data_schema = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["data"]

    assert "brand_factors" in data_schema["properties"]
    assert "brand_elements" not in data_schema["properties"]
    assert "brand_strength" not in data_schema["properties"]
    item = data_schema["properties"]["brand_factors"]["items"]
    assert item["properties"]["role"]["enum"] == ["selected", "competitor"]
    assert set(item["properties"]) == {"brand", "brand_key", "role", "rank", "iqvia", "ubist"}
    assert "pack_desc" in item["properties"]["iqvia"]["properties"]["factors"]["properties"]["values"]["properties"]
    assert item["properties"]["iqvia"]["properties"]["strength"]["additionalProperties"] is False
    example_items = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]["data"]["brand_factors"]
    assert len(example_items) == 6
    assert example_items[0]["role"] == "selected"
    assert example_items[1]["role"] == "competitor"
