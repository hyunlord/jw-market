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
    resolver_calls: list[dict] = []

    def fake_resolve_brand_set(**kwargs):
        resolver_calls.append(kwargs)
        choices = {
            "iqvia_nsa": (
                BrandChoice("리바로", "리바로", 3, True),
                BrandChoice("크레스토", "크레스토", 1, False),
                BrandChoice("리피토", "리피토", 2, False),
            ),
            "ubist": (
                BrandChoice("리바로", "리바로", 2, True),
                BrandChoice("리피토", "리피토", 1, False),
            ),
        }[kwargs["source"]]
        return type("Resolution", (), {"choices": choices})()

    monkeypatch.setattr(deep_analysis, "resolve_brand_set", fake_resolve_brand_set)

    response = TestClient(app).get("/api/deep-analysis/%EB%A6%AC%EB%B0%94%EB%A1%9C")

    assert response.status_code == 200
    data = response.json()["data"]
    assert "brand_elements" not in data
    assert "brand_strength" not in data
    assert "strength_by_source" not in data
    assert [item["brand_key"] for item in data["brand_factors"]["iqvia"]] == ["리바로", "크레스토", "리피토"]
    assert [item["rank"] for item in data["brand_factors"]["iqvia"]] == [3, 1, 2]
    assert [item["brand_key"] for item in data["brand_factors"]["ubist"]] == ["리바로", "리피토"]
    assert [item["rank"] for item in data["brand_factors"]["ubist"]] == [2, 1]
    selected_iqvia = data["brand_factors"]["iqvia"][0]
    assert selected_iqvia["factors"]["available"] is False
    assert selected_iqvia["strength"]["strength_items"] == ["IQVIA 강점"]
    selected_ubist = data["brand_factors"]["ubist"][0]
    assert selected_ubist["factors"]["available"] is True
    assert selected_ubist["factors"]["values"]["seller"] == ["JW중외제약"]
    assert selected_ubist["strength"] == {}
    assert set(selected_iqvia) == {"brand", "brand_key", "role", "rank", "factors", "strength"}
    competitor = data["brand_factors"]["iqvia"][1]
    assert competitor["factors"]["available"] is True
    assert competitor["factors"]["values"]["mfr_name_kor"] == ["AZ"]
    assert competitor["strength"] == {}
    assert {call["source"] for call in resolver_calls} == {"iqvia_nsa", "ubist"}
    assert all(call["rank_by_latest_period"] is True for call in resolver_calls)
    serialized = json.dumps(data, ensure_ascii=False)
    assert "brand_elements" not in serialized
    assert "strength_by_source" not in serialized


def test_strategic_brand_factor_choices_use_ml_id_and_fallback_per_missing_source(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_resolve_brand_set(**kwargs):
        calls.append(kwargs)
        if kwargs["source"] == "ubist":
            return None
        return type(
            "Resolution",
            (),
            {"choices": (BrandChoice("리바로", "리바로", 3, True),)},
        )()

    monkeypatch.setattr(deep_analysis, "resolve_brand_set", fake_resolve_brand_set)

    choices = deep_analysis._resolve_brand_factor_choices(
        {"brand": "리바로", "market_id": "strategy_006"},
        "리바로",
        None,
        {"atc": ["C10A1"]},
    )

    assert choices["iqvia"][0].sales_rank == 3
    assert choices["ubist"][0].sales_rank is None
    assert {call["market_id"] for call in calls} == {"ml_006"}
    assert {call["source"] for call in calls} == {"iqvia_nsa", "ubist"}
    assert all(call["view_name"] == "strategic_ml" for call in calls)


def test_deep_analysis_openapi_documents_source_scoped_brand_factors() -> None:
    app.openapi_schema = None
    schema = app.openapi()

    data_schema = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["data"]

    assert "brand_factors" in data_schema["properties"]
    assert "brand_elements" not in data_schema["properties"]
    assert "brand_strength" not in data_schema["properties"]
    brand_factors = data_schema["properties"]["brand_factors"]
    assert brand_factors["type"] == "object"
    assert set(brand_factors["properties"]) == {"iqvia", "ubist"}
    iqvia_item = brand_factors["properties"]["iqvia"]["items"]
    assert iqvia_item["properties"]["role"]["enum"] == ["selected", "competitor"]
    assert set(iqvia_item["properties"]) == {"brand", "brand_key", "role", "rank", "factors", "strength"}
    assert iqvia_item["properties"]["rank"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert "pack_desc" in iqvia_item["properties"]["factors"]["properties"]["values"]["properties"]
    assert iqvia_item["properties"]["strength"]["anyOf"][0]["additionalProperties"] is False
    assert iqvia_item["properties"]["strength"]["anyOf"][1] == {"type": "object", "maxProperties": 0}
    example = schema["paths"]["/api/deep-analysis/{brand_name}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]["data"]["brand_factors"]
    assert set(example) == {"iqvia", "ubist"}
    assert example["iqvia"][0]["role"] == "selected"
    assert example["ubist"][0]["role"] == "selected"
