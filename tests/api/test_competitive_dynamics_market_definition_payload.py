from __future__ import annotations

import json

from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest
from pipeline.scripts.api.routes import cause as cause_route
from pipeline.scripts.api.dynamic_market import strategic_runtime


def test_cause_route_overlays_cd_market_definition(monkeypatch) -> None:
    raw_payload = {
        "brand": "리바로하이",
        "market_id": "strategy_008",
        "view": "competitive_dynamics",
        "source": "UBIST",
        "measure": "sales",
        "data": {"kpi": {}},
        "market_meta": {
            "view_source_id": "cd_008",
            "market_definition_label": "고혈압/복합",
            "market_definition_full": "리바로하이 리바로브이 시장 정의",
            "atc_codes": ["C11A1", "C9D3"],
            "atc_count": 2,
        },
    }

    monkeypatch.setattr(cause_route, "_brand_exists", lambda brand: True)
    monkeypatch.setattr(
        cause_route,
        "_fetch_cause_rows",
        lambda brand, view, source, measure, market_id=None: [
            {"market_id": "strategy_008", "response_json": json.dumps(raw_payload, ensure_ascii=False)}
        ],
    )
    monkeypatch.setattr(cause_route, "get_display_brand", lambda brand: None)

    response = cause_route.cause(
        "리바로하이",
        view="competitive_dynamics",
        source="UBIST",
        measure="sales",
        market_id=None,
    )

    assert response["market_meta"]["market_definition_label"] == "Statin/ARB/CCB"
    assert response["market_meta"]["market_definition_full"] == "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'"
    assert response["market_meta"]["atc_codes"] == ["Statin/ARB/CCB"]
    assert response["market_meta"]["atc_count"] == 1


def test_strategic_runtime_overlays_cd_market_definition(monkeypatch) -> None:
    market_row = {
        "id": 1,
        "cd_market_id": "cd_008",
        "source": "ubist",
        "measure": "sales",
        "market_size_series": json.dumps({"2026-04": 300.0}),
        "hhi_series_5y": json.dumps({}),
        "brand_ranking_stacked": json.dumps({}),
        "company_ranking_stacked": json.dumps({}),
    }
    brand_rows = [
        {
            "brand_key": "리바로하이",
            "brand_name": "리바로하이",
            "company": "JW중외제약",
            "by_dimension": json.dumps({"atc4": "C9D3"}),
            "value_recent": 1.0,
        }
    ]

    def fake_fetch_all(sql, params):
        return brand_rows

    def fake_fetch_one(sql, params):
        return market_row

    def fake_build_response(**kwargs):
        return {
            "brand": kwargs["brand_row"]["brand_name"],
            "source": kwargs["source"],
            "measure": kwargs["measure"],
            "data": {"kpi": {"target_rank": 1}},
            "market_meta": {
                "view_source_id": kwargs["view_source_id"],
                "market_definition_label": "고혈압/복합",
                "market_definition_full": "리바로하이 리바로브이 시장 정의",
                "atc_codes": ["C11A1", "C9D3"],
                "atc_count": 2,
            },
        }

    monkeypatch.setattr(strategic_runtime.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(strategic_runtime.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        strategic_runtime,
        "_catalog_row",
        lambda market_kind, view_source_id: {"ml_id": "ml_008", "name": "리바로하이/리바로브이"},
    )
    monkeypatch.setattr(strategic_runtime, "_strategic_brand_catalog", lambda: None)
    monkeypatch.setattr(strategic_runtime.cause_builder, "build_response", fake_build_response)

    result = strategic_runtime.build_strategic_payload(
        mart_db="jw_mart",
        ml_id=None,
        cd_market_id="cd_008",
        focus_brand_key="리바로하이",
        source="ubist",
        measure="sales",
        analysis_level=DynamicMarketRequest().filters.analysis_level,
    )

    assert result["market_meta"]["view_source_id"] == "cd_008"
    assert result["market_meta"]["market_definition_label"] == "Statin/ARB/CCB"
    assert result["market_meta"]["market_definition_full"] == "corrected explicit lookup clean(class_2) == 'Statin/ARB/CCB'"
    assert result["market_meta"]["atc_codes"] == ["Statin/ARB/CCB"]
