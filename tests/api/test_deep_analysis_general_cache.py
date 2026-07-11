from __future__ import annotations

from datetime import datetime
import json
import math
from typing import Any

from fastapi.testclient import TestClient

from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import deep_analysis
from pipeline.scripts.api.dynamic_market.response_cache import DynamicMarketOverloadedError


def _row(scope: str, *, atc4: str | None = None, events: list[dict] | None = None) -> dict[str, Any]:
    return {
        "response_json": json.dumps(
            {
                "brand": "멀티브랜드",
                "brand_key": "멀티브랜드",
                "market_id": "ml_001" if scope == "strategic" else f"general:{atc4}",
                "data": {
                    "forecast": {"by_combo": {f"{scope}.sales": {"period_unit": "월", "forecast_periods": []}}},
                    "simulation": {"by_combo": {f"{scope}.sales": {"kind": scope}}},
                    "events": events or [],
                    "shared": {"kept": True},
                },
                "market_meta": {"scope": scope, "atc4_code": atc4},
            },
            ensure_ascii=False,
        ),
        "brand": "멀티브랜드",
        "brand_key": "멀티브랜드",
        "brand_factors": json.dumps({"atc": [atc4] if atc4 else [], "ubist": {}, "iqvia": {}}, ensure_ascii=False),
        "updated_at": "2026-07-09T09:00:00+09:00",
        "atc4_code": atc4,
    }


def _stub_auxiliary(monkeypatch) -> None:
    monkeypatch.setattr(deep_analysis, "_load_ai_analysis", lambda _brand: {"summary": "ai"})
    monkeypatch.setattr(deep_analysis, "_load_ai_analysis_variants", lambda _brand: ({"available": False}, {"available": False}))
    monkeypatch.setattr(deep_analysis, "_load_cached_brand_elements", lambda _brand_keys: {})
    monkeypatch.setattr(deep_analysis, "_load_brand_strength_by_source", lambda _brand_keys: {})
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [])
    monkeypatch.setattr(
        deep_analysis,
        "_resolve_brand_factor_choices",
        lambda row, requested_brand, atc4, selected_factors: {"iqvia": (), "ubist": ()},
    )


def test_deep_analysis_defaults_to_strategic_view(monkeypatch) -> None:
    queries: list[str] = []

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        queries.append(sql)
        if "cache_deep_analysis" in sql:
            return _row("strategic", events=[{"id": 1}])
        return None

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis, "_strategic_row_from_mart", lambda _brand: _row("strategic"))
    _stub_auxiliary(monkeypatch)

    payload = deep_analysis.deep_analysis("멀티브랜드")

    assert payload["market_id"] == "ml_001"
    assert payload["data"]["forecast"]["by_combo"] == {"strategic.sales": {"period_unit": "월", "forecast_periods": []}}
    assert not any("cache_deep_analysis_general" in query for query in queries)


def test_deep_analysis_general_view_reuses_shared_sections_and_replaces_view_dependent_parts(monkeypatch) -> None:
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_general" in sql:
            return _row("general", atc4="A10N3")
        if "cache_deep_analysis" in sql:
            return _row("strategic", events=[{"id": 1}])
        return None

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    _stub_auxiliary(monkeypatch)
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [{"id": 1}])

    payload = deep_analysis.deep_analysis("멀티브랜드", view="general")

    assert payload["market_id"] == "general:A10N3"
    assert payload["data"]["events"] == [{"id": 1}]
    assert payload["data"]["forecast"]["by_combo"] == {"general.sales": {"period_unit": "월", "forecast_periods": []}}
    assert payload["data"]["simulation"]["by_combo"] == {"general.sales": {"kind": "general"}}


def test_deep_analysis_general_view_builds_lightweight_mart_payload_without_on_demand_generation(monkeypatch) -> None:
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis" in sql:
            return None
        return None

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        assert "mart_general_brand_metric" in sql
        return [
            {
                "brand_key": "멀티브랜드",
                "brand_name": "멀티브랜드",
                "atc4_code": "B01C0",
                "atc4_desc": "B 시장",
                "source": "ubist",
                "measure": "sales",
                "metric_history": json.dumps(
                    {"2026-01": {"raw_value": 10, "ms": 1.5}, "2026-02": {"raw_value": 12, "ms": 1.7}},
                    ensure_ascii=False,
                ),
                "unit_label": "원",
                "is_jw": 0,
                "is_target": 0,
                "computed_at": datetime(2026, 7, 1),
            }
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)
    _stub_auxiliary(monkeypatch)

    payload = deep_analysis.deep_analysis("멀티브랜드", view="general")

    combo = payload["data"]["forecast"]["by_combo"]["UBIST.sales"]
    assert payload["market_id"] == "general:B01C0"
    assert payload["data"]["events"] == []
    assert combo["history_periods"] == ["2026-01", "2026-02"]
    assert combo["brands"][0]["history_values"] == [10.0, 12.0]
    assert combo["forecast_periods"] == []


def test_deep_analysis_general_view_for_strategic_brand_uses_only_general_mart_columns(monkeypatch) -> None:
    strategic_row = _row("strategic", events=[{"id": 1}])
    strategic_payload = json.loads(strategic_row["response_json"])
    strategic_payload["market_meta"]["is_jw"] = True
    strategic_row["response_json"] = json.dumps(strategic_payload, ensure_ascii=False)

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_general" in sql:
            return None
        if "mart_strategic_ml_brand_metric" in sql:
            return {"is_jw": 1, "is_target": 1}
        if "cache_deep_analysis" in sql:
            return strategic_row
        return None

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        assert "mart_general_brand_metric" in sql
        assert "is_jw" not in sql
        assert "is_target" not in sql
        return [
            {
                "brand_key": "JW브랜드",
                "brand_name": "JW브랜드",
                "atc4_code": "B01C0",
                "atc4_desc": "B 시장",
                "source": "ubist",
                "measure": "sales",
                "metric_history": json.dumps({"2026-02": {"raw_value": 12, "ms": 1.7}}, ensure_ascii=False),
                "unit_label": "원",
                "computed_at": datetime(2026, 7, 1),
            }
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)
    _stub_auxiliary(monkeypatch)

    payload = deep_analysis.deep_analysis("JW브랜드", view="general")

    assert payload["market_meta"]["is_jw"] is True
    assert payload["market_meta"]["is_target"] is True


def test_deep_analysis_general_view_rejects_removed_atc4_parameter(monkeypatch) -> None:
    response = TestClient(app).get("/api/deep-analysis/%EB%A9%80%ED%8B%B0%EB%B8%8C%EB%9E%9C%EB%93%9C?view=general&atc4=A10N3")

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unsupported_query_parameter"


def test_deep_analysis_general_view_returns_404_only_when_brand_is_outside_general_mart(monkeypatch) -> None:
    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])
    _stub_auxiliary(monkeypatch)

    response = TestClient(app).get("/api/deep-analysis/%EB%AF%B8%EC%83%9D%EC%84%B1?view=general")

    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "brand_not_found", "brand": "미생성"}


def test_strategic_view_uses_mart_row_without_legacy_base_lookup(monkeypatch) -> None:
    strategic_row = _row("strategic", events=[])
    seen: list[str] = []

    monkeypatch.setattr(deep_analysis, "_strategic_row_from_mart", lambda _brand: strategic_row)
    _stub_auxiliary(monkeypatch)
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda brand: seen.append(brand) or [{"id": "event-1"}])

    payload = deep_analysis.deep_analysis("멀티브랜드", view="strategic")

    assert payload["market_id"] == "ml_001"
    assert payload["data"]["events"] == [{"id": "event-1"}]
    assert seen == ["멀티브랜드"]


def test_general_view_loads_shared_events_without_legacy_base_lookup(monkeypatch) -> None:
    general_row = _row("general", atc4="A10N3", events=[])

    monkeypatch.setattr(deep_analysis, "_fetch_general_deep_analysis_row", lambda _brand: general_row)
    _stub_auxiliary(monkeypatch)
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [{"id": "event-1"}])

    payload = deep_analysis.deep_analysis("멀티브랜드", view="general")

    assert payload["data"]["events"] == [{"id": "event-1"}]


def test_strategic_view_returns_429_when_expensive_section_capacity_is_full(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis,
        "_strategic_row_from_mart",
        lambda _brand: (_ for _ in ()).throw(DynamicMarketOverloadedError("busy")),
    )

    response = TestClient(app).get("/api/deep-analysis/%EB%A9%80%ED%8B%B0%EB%B8%8C%EB%9E%9C%EB%93%9C")

    assert response.status_code == 429
    assert response.json()["detail"] == {"error": "deep_analysis_busy"}


def test_deep_analysis_normalizes_non_finite_section_values(monkeypatch) -> None:
    strategic_row = _row("strategic", events=[])
    payload = json.loads(strategic_row["response_json"])
    payload["data"]["forecast"]["score"] = math.nan
    strategic_row["response_json"] = json.dumps(payload)
    monkeypatch.setattr(deep_analysis, "_strategic_row_from_mart", lambda _brand: strategic_row)
    _stub_auxiliary(monkeypatch)

    result = deep_analysis.deep_analysis("멀티브랜드")

    assert result["data"]["forecast"]["score"] is None
