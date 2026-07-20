from __future__ import annotations

from datetime import datetime
import json
import math
from typing import Any

from fastapi.testclient import TestClient

from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import deep_analysis
from pipeline.scripts.api.dynamic_market.response_cache import DynamicMarketOverloadedError
from pipeline.scripts.utils.atc4 import atc4_source_aliases, normalize_atc4


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
    monkeypatch.setattr(deep_analysis, "_strategic_brand_flags", lambda _brand: (False, False))
    monkeypatch.setattr(
        deep_analysis,
        "_resolve_brand_factor_choices",
        lambda row, requested_brand, atc4, selected_factors: (
            {"iqvia": (), "ubist": ()},
            {
                "iqvia": {"available": True, "reason": None},
                "ubist": {"available": True, "reason": None},
            },
        ),
    )


def test_normalize_atc4_uses_one_canonical_zero_pad_rule() -> None:
    assert normalize_atc4("C10C") == "C10C0"
    assert normalize_atc4("C10C0") == "C10C0"
    assert normalize_atc4("C1D") == "C01D0"
    assert normalize_atc4("G4C2") == "G04C2"
    assert normalize_atc4("A10N1") == "A10N1"
    assert atc4_source_aliases("C01D0") == ("C01D0", "C1D0", "C01D", "C1D")


def test_general_metric_lookup_uses_source_native_row_for_normalized_market(monkeypatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        seen.append((sql, params))
        return [
            {
                "brand_key": "리바로젯",
                "brand_name": "리바로젯",
                "atc4_code": "C10C0",
                "source": "iqvia_nsa",
            }
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    rows = deep_analysis._fetch_general_metric_rows(
        "리바로젯",
        atc4="C10C",
        source="iqvia_nsa",
    )

    assert [row["atc4_code"] for row in rows] == ["C10C0"]
    assert len(seen) == 1
    sql, params = seen[0]
    assert "source = %s" in sql
    assert "atc4_code = %s" not in sql
    assert params == ["리바로젯", "리바로젯", "iqvia_nsa"]


def test_general_cache_is_bypassed_when_source_codes_share_normalized_market(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis.db,
        "fetch_all",
        lambda *_args, **_kwargs: [
            {"atc4_code": "C10C", "source_computed_at": datetime(2026, 7, 1)},
            {"atc4_code": "C10C0", "source_computed_at": datetime(2026, 7, 1)},
            {"atc4_code": "C10A1", "source_computed_at": datetime(2026, 7, 2)},
        ],
    )
    row = {
        "atc4_code": "C10C",
        "source_computed_at": None,
        "is_stale": 0,
    }

    assert deep_analysis._general_cache_row_fresh(row, "리바로젯", "C10C") is False


def test_general_cache_remains_usable_for_single_raw_market_code(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis.db,
        "fetch_all",
        lambda *_args, **_kwargs: [
            {"atc4_code": "A10N1", "source_computed_at": datetime(2026, 7, 1)},
            {"atc4_code": "C10A1", "source_computed_at": datetime(2026, 7, 2)},
        ],
    )
    row = {
        "atc4_code": "A10N1",
        "source_computed_at": datetime(2026, 7, 1),
        "is_stale": 0,
    }

    assert deep_analysis._general_cache_row_fresh(row, "가드렛", "A10N1") is True


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


def test_deep_analysis_general_view_refreshes_jw_identity_from_strategic_mart(monkeypatch) -> None:
    general_row = _row("general", atc4="C10A1")
    general_payload = json.loads(general_row["response_json"])
    general_payload["market_meta"]["is_jw"] = False
    general_payload["market_meta"]["is_target"] = False
    general_row["response_json"] = json.dumps(general_payload, ensure_ascii=False)

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_general" in sql:
            return general_row
        if "mart_strategic_ml_brand_metric" in sql:
            return {"is_jw": 1, "is_target": 1}
        return None

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    _stub_auxiliary(monkeypatch)
    monkeypatch.setattr(deep_analysis, "_strategic_brand_flags", lambda _brand: (True, True))

    payload = deep_analysis.deep_analysis("JW브랜드", view="general")

    assert payload["market_meta"]["is_jw"] is True
    assert payload["market_meta"]["is_target"] is False


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


def test_general_mart_payload_merges_zero_pad_atc4_sources_without_changing_home_market_id(monkeypatch) -> None:
    rows = [
        {
            "brand_key": "리바로젯",
            "brand_name": "리바로젯",
            "atc4_code": "C10C",
            "atc4_desc": "UBIST C10C",
            "source": "ubist",
            "measure": "sales",
            "metric_history": json.dumps({"2026-05": {"raw_value": 100, "ms": 4.2}}, ensure_ascii=False),
            "unit_label": "원",
            "computed_at": datetime(2026, 7, 1),
        },
        {
            "brand_key": "리바로젯",
            "brand_name": "리바로젯",
            "atc4_code": "C10C0",
            "atc4_desc": "IQVIA C10C0",
            "source": "iqvia_nsa",
            "measure": "sales",
            "metric_history": json.dumps({"2026-Q1": {"raw_value": 90, "ms": 4.0}}, ensure_ascii=False),
            "unit_label": "원",
            "computed_at": datetime(2026, 7, 1),
        },
    ]
    monkeypatch.setattr(deep_analysis, "_fetch_general_metric_rows", lambda *_args, **_kwargs: rows)

    result = deep_analysis._general_row_from_mart("리바로젯")

    assert result is not None
    payload = json.loads(result["response_json"])
    assert payload["market_id"] == "general:C10C"
    assert payload["market_meta"]["atc4_code"] == "C10C"
    assert payload["market_meta"]["sources"] == ["IQVIA", "UBIST"]
    assert payload["available_combos"] == ["IQVIA.sales", "UBIST.sales"]
    assert payload["data"]["forecast"]["by_combo"]["IQVIA.sales"]["history_periods"] == ["2026-Q1"]
    assert payload["data"]["forecast"]["by_combo"]["UBIST.sales"]["history_periods"] == ["2026-05"]


def test_general_mart_payload_keeps_single_code_market_behavior(monkeypatch) -> None:
    rows = [
        {
            "brand_key": "가드렛",
            "brand_name": "가드렛",
            "atc4_code": "A10N1",
            "atc4_desc": "A10N1",
            "source": source,
            "measure": "sales",
            "metric_history": json.dumps({period: {"raw_value": value, "ms": 1.0}}, ensure_ascii=False),
            "unit_label": "원",
            "computed_at": datetime(2026, 7, 1),
        }
        for source, period, value in (("ubist", "2026-05", 100), ("iqvia_nsa", "2026-Q1", 90))
    ]
    monkeypatch.setattr(deep_analysis, "_fetch_general_metric_rows", lambda *_args, **_kwargs: rows)

    result = deep_analysis._general_row_from_mart("가드렛")

    assert result is not None
    payload = json.loads(result["response_json"])
    assert payload["market_id"] == "general:A10N1"
    assert payload["market_meta"]["atc4_code"] == "A10N1"
    assert payload["available_combos"] == ["IQVIA.sales", "UBIST.sales"]


def test_general_mart_payload_does_not_merge_distinct_normalized_markets(monkeypatch) -> None:
    rows = [
        {
            "brand_key": "다중시장",
            "brand_name": "다중시장",
            "atc4_code": atc4,
            "atc4_desc": atc4,
            "source": source,
            "measure": "sales",
            "metric_history": json.dumps({"2026-05": {"raw_value": value, "ms": 1.0}}, ensure_ascii=False),
            "unit_label": "원",
            "computed_at": datetime(2026, 7, 1),
        }
        for atc4, source, value in (("C10C", "ubist", 100), ("C10C0", "iqvia_nsa", 90), ("C10A1", "iqvia_nsa", 80))
    ]
    monkeypatch.setattr(deep_analysis, "_fetch_general_metric_rows", lambda *_args, **_kwargs: rows)

    result = deep_analysis._general_row_from_mart("다중시장")

    assert result is not None
    payload = json.loads(result["response_json"])
    assert payload["market_id"] == "general:C10C"
    assert payload["available_combos"] == ["IQVIA.sales", "UBIST.sales"]
    assert payload["market_meta"]["market_count"] == 1


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
    monkeypatch.setattr(deep_analysis, "_strategic_brand_flags", lambda _brand: (True, True))

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


def test_strategic_brand_flags_use_the_display_brand_catalog() -> None:
    assert deep_analysis._strategic_brand_flags("리바로") == (True, False)
    assert deep_analysis._strategic_brand_flags("리피토") == (False, False)
