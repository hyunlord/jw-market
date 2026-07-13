from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from typing import Any

from fastapi.testclient import TestClient
import pymysql
import pytest

from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import deep_analysis
from pipeline.scripts.api import deep_analysis_context, deep_analysis_serving
from pipeline.scripts.api.deep_analysis_context import (
    DeepAnalysisContext,
    DeepAnalysisContextError,
    public_source_labels,
    resolve_deep_analysis_context,
)


def _catalog_row(
    *,
    market_id: str,
    market_name: str = "시장",
    data_source: str = "UBIST",
    brand_key: str = "선택브랜드",
) -> dict[str, Any]:
    return {
        "brand_key": brand_key,
        "brand_name": brand_key,
        "market_id": market_id,
        "market_name": market_name,
        "data_source": data_source,
    }


def _context(*, has_market_data: bool = True) -> DeepAnalysisContext:
    return DeepAnalysisContext(
        brand_key="선택브랜드",
        brand_name="선택브랜드",
        view_kind="strategic_ml",
        market_id="ml_003",
        market_name="당뇨 OAD",
        source="ubist",
        db_source="ubist",
        in_catalog=True,
        has_market_data=has_market_data,
        market_allowed_sources=("ubist",),
        brand_available_sources=("iqvia_nsa",),
    )


def test_public_source_labels_accepts_one_source_and_keeps_preferred_order() -> None:
    assert public_source_labels("iqvia_nsa") == ["IQVIA"]
    assert public_source_labels(("IQVIA", "ubist")) == ["UBIST", "IQVIA"]


def test_strategic_ml_context_filters_excluded_catalog_memberships(monkeypatch) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def fake_fetch_all(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        if "catalog_strategic_brand" in sql:
            return [_catalog_row(market_id="ml_003")]
        if "mart_strategic_ml_brand_metric" in sql:
            return [{"market_id": "ml_003", "source": "ubist"}]
        if "mart_general_brand_metric" in sql:
            return [{"source": "iqvia_nsa"}]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)

    context = resolve_deep_analysis_context(
        brand="선택브랜드",
        view_kind="strategic_ml",
        market_id="ml_003",
        source="ubist",
    )

    assert context.market_id == "ml_003"
    assert context.has_market_data is True
    catalog_sql = next(sql for sql, _params in calls if "catalog_strategic_brand" in sql)
    assert "is_excluded" in catalog_sql
    assert "= 0" in catalog_sql


def test_strategic_cd_uses_cd_catalog_and_mart(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, _params: tuple[Any, ...]) -> list[dict[str, Any]]:
        calls.append(sql)
        if "catalog_strategic_brand" in sql:
            return [_catalog_row(market_id="cd_007")]
        if "mart_strategic_cd_brand_metric" in sql:
            return [{"market_id": "cd_007", "source": "ubist"}]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)

    context = resolve_deep_analysis_context(
        brand="선택브랜드",
        view_kind="strategic_cd",
        market_id="cd_007",
        source="ubist",
    )

    assert context.view_kind == "strategic_cd"
    assert any("catalog_cd_market" in sql for sql in calls)
    assert any("mart_strategic_cd_brand_metric" in sql for sql in calls)


def test_market_omission_is_rejected_when_context_is_ambiguous(monkeypatch) -> None:
    def fake_fetch_all(sql: str, _params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if "catalog_strategic_brand" in sql:
            return [
                _catalog_row(market_id="ml_003"),
                _catalog_row(market_id="ml_009", market_name="다른 시장"),
            ]
        if "mart_strategic_ml_brand_metric" in sql:
            return [
                {"market_id": "ml_003", "source": "ubist"},
                {"market_id": "ml_009", "source": "ubist"},
            ]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)

    with pytest.raises(DeepAnalysisContextError) as exc_info:
        resolve_deep_analysis_context(
            brand="선택브랜드",
            view_kind="strategic_ml",
            market_id=None,
            source="ubist",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error == "ambiguous_market_context"
    assert [item["market_id"] for item in exc_info.value.available_contexts] == ["ml_003", "ml_009"]


def test_catalog_member_without_sales_rows_resolves_as_no_market_data(monkeypatch) -> None:
    def fake_fetch_all(sql: str, _params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if "catalog_strategic_brand" in sql:
            return [_catalog_row(market_id="ml_003")]
        if "mart_strategic_ml_brand_metric" in sql:
            return []
        if "mart_general_brand_metric" in sql:
            return [{"source": "iqvia_nsa"}]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)

    context = resolve_deep_analysis_context(
        brand="글리펜",
        view_kind="strategic_ml",
        market_id="ml_003",
        source="ubist",
    )

    assert context.in_catalog is True
    assert context.has_market_data is False
    assert context.brand_available_sources == ("iqvia_nsa",)


def test_catalog_both_source_exposes_ubist_and_iqvia_contexts(monkeypatch) -> None:
    def fake_fetch_all(sql: str, _params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if "catalog_strategic_brand" in sql:
            return [_catalog_row(market_id="ml_003", data_source="both")]
        return []

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)

    contexts = deep_analysis_context._strategic_contexts("선택브랜드", "strategic_ml")

    assert [(item.source, item.db_source) for item in contexts] == [
        ("iqvia", "iqvia_nsa"),
        ("ubist", "ubist"),
    ]


def test_general_context_uses_explicit_atc4_and_source(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis_context.db,
        "fetch_all",
        lambda sql, _params: [
            {
                "brand_key": "일반브랜드",
                "brand_name": "일반브랜드",
                "atc4_code": "N02B2",
                "market_name": "진통제",
                "source": "ubist",
            },
            {
                "brand_key": "일반브랜드",
                "brand_name": "일반브랜드",
                "atc4_code": "N02B2",
                "market_name": "진통제",
                "source": "iqvia_nsa",
            },
        ]
        if "mart_general_brand_metric" in sql
        else [],
    )

    context = resolve_deep_analysis_context(
        brand="일반브랜드",
        view_kind="general",
        market_id="n02b2",
        source="iqvia",
    )

    assert context.market_id == "N02B2"
    assert context.source == "iqvia"
    assert context.db_source == "iqvia_nsa"


def test_source_omission_is_rejected_for_dual_source_market(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis_context.db,
        "fetch_all",
        lambda sql, _params: [
            {
                "brand_key": "일반브랜드",
                "brand_name": "일반브랜드",
                "atc4_code": "N02B2",
                "market_name": "진통제",
                "source": "ubist",
            },
            {
                "brand_key": "일반브랜드",
                "brand_name": "일반브랜드",
                "atc4_code": "N02B2",
                "market_name": "진통제",
                "source": "iqvia_nsa",
            },
        ]
        if "mart_general_brand_metric" in sql
        else [],
    )

    with pytest.raises(DeepAnalysisContextError) as exc_info:
        resolve_deep_analysis_context(
            brand="일반브랜드",
            view_kind="general",
            market_id="N02B2",
            source=None,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error == "ambiguous_source_context"
    assert {item["source"] for item in exc_info.value.available_contexts} == {"ubist", "iqvia"}


def test_general_compact_lookup_rejects_multiple_brand_keys(monkeypatch) -> None:
    def fake_fetch_all(sql: str, _params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if "brand_key = %s OR brand_name = %s" in sql:
            return []
        return [
            {
                "brand_key": "브랜드 A",
                "brand_name": "브랜드 A",
                "atc4_code": "N02B2",
                "market_name": "진통제",
                "source": "ubist",
            },
            {
                "brand_key": "브랜드A",
                "brand_name": "브랜드A",
                "atc4_code": "N02B2",
                "market_name": "진통제",
                "source": "ubist",
            },
        ]

    monkeypatch.setattr(deep_analysis_context.db, "fetch_all", fake_fetch_all)

    with pytest.raises(DeepAnalysisContextError) as exc_info:
        resolve_deep_analysis_context(
            brand="브랜드 A",
            view_kind="general",
            market_id="N02B2",
            source="ubist",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.error == "ambiguous_brand"


def test_formal_general_source_filter_marks_empty_forecast_not_generated() -> None:
    context = DeepAnalysisContext(
        brand_key="일반브랜드",
        brand_name="일반브랜드",
        view_kind="general",
        market_id="N02B2",
        market_name="진통제",
        source="iqvia",
        db_source="iqvia_nsa",
        in_catalog=True,
        has_market_data=True,
        market_allowed_sources=("iqvia",),
        brand_available_sources=("iqvia_nsa",),
    )
    payload = {
        "data": {
            "forecast": {"by_combo": {"UBIST.sales": {"series": [1]}}},
            "simulation": {"by_combo": {"UBIST.sales": {"series": [1]}}},
        },
        "market_meta": "invalid legacy value",
    }

    deep_analysis._scope_formal_payload(payload, context)

    assert payload["data"]["forecast"] == []
    assert payload["data"]["simulation"] == []
    assert payload["data"]["forecast_meta"]["status"] == "not_generated"
    assert payload["market_meta"]["market_id"] == "N02B2"


def test_formal_no_market_data_response_is_200_with_source_context(monkeypatch) -> None:
    monkeypatch.setattr(deep_analysis, "resolve_deep_analysis_context", lambda **_kwargs: _context(has_market_data=False))
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [])
    monkeypatch.setattr(
        deep_analysis,
        "_load_canonical_ai_analysis_variants",
        lambda _brand: ({"available": False, "reason": "not_generated"}, {"available": False, "reason": "not_generated"}),
    )
    monkeypatch.setattr(deep_analysis, "_formal_brand_factors", lambda _brand, _context: {"iqvia": [], "ubist": []})

    response = TestClient(app).get(
        "/api/deep-analysis/%EA%B8%80%EB%A6%AC%ED%8E%9C?view_kind=strategic_ml&market_id=ml_003&source=ubist"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["forecast"] == []
    assert data["simulation"] == []
    assert data["events"] == []
    assert data["events_meta"] == {
        "status": "no_news",
        "reason": "해당 브랜드 관련 뉴스 없음",
        "bundle_available": False,
    }
    assert data["data_meta"] == {
        "status": "no_market_data",
        "reason": "해당 시장은 UBIST 기준이나 이 브랜드는 IQVIA 데이터만 존재",
        "market_allowed_sources": ["ubist"],
        "brand_available_sources": ["iqvia_nsa"],
        "in_catalog": True,
    }
    assert response.json()["market_meta"]["available"] is False
    assert response.json()["market_meta"]["reason"] == "brand_not_in_source"
    assert response.json()["market_meta"]["available_sources"] == ["IQVIA"]


def test_formal_no_market_data_does_not_claim_source_mismatch_when_source_exists() -> None:
    context = replace(
        _context(has_market_data=False),
        source="iqvia",
        db_source="iqvia_nsa",
        brand_available_sources=("iqvia_nsa",),
    )

    market_meta = deep_analysis._formal_market_meta(context)

    assert "available" not in market_meta
    assert "reason" not in market_meta
    assert "available_sources" not in market_meta


def test_formal_cd_context_returns_200_when_native_sections_are_not_generated(monkeypatch) -> None:
    context = DeepAnalysisContext(
        brand_key="CD브랜드",
        brand_name="CD브랜드",
        view_kind="strategic_cd",
        market_id="cd_007",
        market_name="CD 시장",
        source="ubist",
        db_source="ubist",
        in_catalog=True,
        has_market_data=True,
        market_allowed_sources=("ubist",),
        brand_available_sources=("ubist",),
    )
    monkeypatch.setattr(deep_analysis, "resolve_deep_analysis_context", lambda **_kwargs: context)
    monkeypatch.setattr(deep_analysis, "_load_formal_forecast_sections", lambda _context: (None, None))
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [])
    monkeypatch.setattr(
        deep_analysis,
        "_load_canonical_ai_analysis_variants",
        lambda _brand: ({"available": False, "reason": "not_generated"}, {"available": False, "reason": "not_generated"}),
    )
    monkeypatch.setattr(deep_analysis, "_formal_brand_factors", lambda _brand, _context: {"iqvia": [], "ubist": []})

    response = TestClient(app).get(
        "/api/deep-analysis/CD%EB%B8%8C%EB%9E%9C%EB%93%9C?view_kind=strategic_cd&market_id=cd_007&source=ubist"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["view_kind"] == "strategic_cd"
    assert payload["market_id"] == "cd_007"
    assert payload["data"]["forecast"] == []
    assert payload["data"]["forecast_meta"]["status"] == "not_generated"


def test_missing_future_forecast_table_is_an_optional_section(monkeypatch) -> None:
    def missing_table(*_args, **_kwargs):
        raise pymysql.err.ProgrammingError(1146, "deep_forecast_block does not exist")

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_one", missing_table)

    assert deep_analysis._load_formal_forecast_sections(_context()) == (None, None)


def test_forecast_adapter_uses_composite_key_and_horizon_fallback(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        calls.append((sql, params))
        if "deep_forecast_horizon" in sql:
            measure = params[-1]
            return {
                "measure": measure,
                "forecast_horizon_json": json.dumps({"series": [measure]}),
            }
        return None

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_one", fake_fetch_one)

    forecast, simulation = deep_analysis._load_formal_forecast_sections(_context())

    assert forecast == {
        "by_combo": {
            "UBIST.sales": {"series": ["sales"]},
            "UBIST.volume": {"series": ["volume"]},
        }
    }
    assert simulation is None
    block_calls = [(sql, params) for sql, params in calls if "deep_forecast_block" in sql]
    horizon_calls = [(sql, params) for sql, params in calls if "deep_forecast_horizon" in sql]
    assert len(block_calls) == 1
    assert "brand_key = %s AND source = %s AND market_id = %s" in block_calls[0][0]
    assert "view_kind" not in block_calls[0][0]
    assert block_calls[0][1] == ["선택브랜드", "ubist", "ml_003"]
    assert len(horizon_calls) == 2
    assert all("brand_key" not in sql for sql, _params in horizon_calls)
    assert all("market_id = %s AND source = %s AND measure = %s" in sql for sql, _params in horizon_calls)
    assert [params for _sql, params in horizon_calls] == [
        ["ml_003", "ubist", "sales"],
        ["ml_003", "ubist", "volume"],
    ]


def test_forecast_adapter_uses_db_source_for_iqvia_natural_keys(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        calls.append((sql, params))
        return None

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_one", fake_fetch_one)
    context = replace(_context(), source="iqvia", db_source="iqvia_nsa")

    assert deep_analysis_serving.load_forecast_records(context) == (None, None)
    assert calls[0][1] == ["선택브랜드", "iqvia_nsa", "ml_003"]
    assert [params for _sql, params in calls[1:]] == [
        ["ml_003", "iqvia_nsa", "counting_unit"],
        ["ml_003", "iqvia_nsa", "dosage_unit"],
        ["ml_003", "iqvia_nsa", "sales"],
        ["ml_003", "iqvia_nsa", "unit"],
    ]


def test_missing_future_strength_table_is_an_optional_section(monkeypatch) -> None:
    def missing_table(*_args, **_kwargs):
        raise pymysql.err.ProgrammingError(1146, "agent3_brand_strength_market does not exist")

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_all", missing_table)

    assert deep_analysis._load_market_strength(["선택브랜드"], _context()) == {}


def test_market_strength_adapter_uses_source_market_and_view(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        return [
            {
                "brand_key": "선택브랜드",
                "strength_summary_json": json.dumps(
                    {"profile_display": {}, "strength_items": [{"title": "강점"}], "limitations": []}
                ),
            }
        ]

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_all", fake_fetch_all)

    result = deep_analysis._load_market_strength(["선택브랜드"], _context())

    assert result["선택브랜드"]["ubist"]["strength_items"] == [{"title": "강점"}]
    assert len(calls) == 1
    sql, params = calls[0]
    assert "agent3_brand_strength_market" in sql
    assert "source = %s AND market_id = %s AND view_kind = %s" in sql
    assert params == ["선택브랜드", "ubist", "ml_003", "market_landscape"]


def test_market_strength_adapter_maps_competitive_dynamics_view(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        deep_analysis_serving.db,
        "fetch_all",
        lambda _sql, params: calls.append(params) or [],
    )
    context = replace(_context(), view_kind="strategic_cd", market_id="cd_007")

    assert deep_analysis._load_market_strength(["선택브랜드"], context) == {}
    assert calls == [["선택브랜드", "ubist", "cd_007", "competitive_dynamics"]]


def test_general_strength_adapter_keeps_source_scoped_table(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        return []

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_all", fake_fetch_all)
    context = replace(_context(), view_kind="general", market_id="C10A1")

    assert deep_analysis._load_market_strength(["선택브랜드"], context) == {}
    sql, params = calls[0]
    assert "agent3_brand_strength_source" in sql
    assert "market_id" not in sql
    assert params == ["선택브랜드", "ubist"]


def test_formal_strategy_never_falls_back_to_general_strength(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        calls.append(sql)
        return []

    monkeypatch.setattr(deep_analysis_serving.db, "fetch_all", fake_fetch_all)

    assert deep_analysis._load_market_strength(["선택브랜드"], _context()) == {}
    assert len(calls) == 1
    assert "agent3_brand_strength_market" in calls[0]
    assert "agent3_brand_strength_source" not in calls[0]


def test_formal_contract_does_not_read_legacy_ai_variant(monkeypatch) -> None:
    context = _context(has_market_data=True)
    monkeypatch.setattr(deep_analysis, "resolve_deep_analysis_context", lambda **_kwargs: context)
    monkeypatch.setattr(
        deep_analysis,
        "_compose_formal_context_payload",
        lambda _brand, _context: (
            {
                "brand": "선택브랜드",
                "data": {"forecast": [], "simulation": [], "events": []},
                "market_meta": {},
            },
            {
                "brand": "선택브랜드",
                "brand_key": "선택브랜드",
                "brand_factors": json.dumps({}),
                "updated_at": datetime(2026, 7, 12),
            },
        ),
    )
    monkeypatch.setattr(deep_analysis, "_load_ai_analysis", lambda _brand: pytest.fail("legacy ai_analysis_json queried"))
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [])
    monkeypatch.setattr(
        deep_analysis,
        "_load_canonical_ai_analysis_variants",
        lambda _brand: ({"headline": "short"}, {"headline": "long"}),
    )
    monkeypatch.setattr(deep_analysis, "_formal_brand_factors", lambda _brand, _context: {"iqvia": [], "ubist": []})

    response = TestClient(app).get(
        "/api/deep-analysis/%EC%84%A0%ED%83%9D%EB%B8%8C%EB%9E%9C%EB%93%9C?view_kind=strategic_ml&market_id=ml_003&source=ubist"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ai_analysis"] == {"headline": "short"}
    assert data["ai_analysis_short"] == {"headline": "short"}
    assert data["ai_analysis_long"] == {"headline": "long"}
    assert data["forecast_meta"]["status"] == "not_generated"
    assert data["strength_meta"]["status"] == "not_generated"


def test_formal_and_legacy_view_conflict_is_422() -> None:
    response = TestClient(app).get(
        "/api/deep-analysis/%EC%84%A0%ED%83%9D%EB%B8%8C%EB%9E%9C%EB%93%9C?view=general&view_kind=strategic_ml&market_id=ml_003&source=ubist"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "conflicting_view_contract"


def test_invalid_formal_context_returns_available_contexts(monkeypatch) -> None:
    error = DeepAnalysisContextError(
        status_code=404,
        error="market_membership_not_found",
        message="brand is not a member of the requested market",
        available_contexts=(
            {
                "view_kind": "strategic_ml",
                "market_id": "ml_003",
                "market_name": "당뇨 OAD",
                "source": "ubist",
                "has_market_data": True,
            },
        ),
    )

    def raise_error(**_kwargs):
        raise error

    monkeypatch.setattr(deep_analysis, "resolve_deep_analysis_context", raise_error)
    response = TestClient(app).get(
        "/api/deep-analysis/%EC%84%A0%ED%83%9D%EB%B8%8C%EB%9E%9C%EB%93%9C?view_kind=strategic_ml&market_id=ml_999&source=ubist"
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "market_membership_not_found"
    assert detail["available_contexts"][0]["market_id"] == "ml_003"


def test_formal_openapi_exposes_new_query_contract() -> None:
    operation = app.openapi()["paths"]["/api/deep-analysis/{brand_name}"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}

    assert set(("view", "view_kind", "market_id", "source")) <= parameters.keys()
    view_schema = parameters["view_kind"]["schema"]
    source_schema = parameters["source"]["schema"]
    view_enum = next(item["enum"] for item in view_schema["anyOf"] if "enum" in item)
    source_enum = next(item["enum"] for item in source_schema["anyOf"] if "enum" in item)

    assert view_enum == ["general", "strategic_ml", "strategic_cd"]
    assert source_enum == ["ubist", "iqvia"]
