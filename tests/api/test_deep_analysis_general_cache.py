from __future__ import annotations

from datetime import datetime
import json
import logging
import math
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from pipeline.scripts.api.main import app
from pipeline.scripts.api.deep_analysis_serving import ForecastBlock
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
    monkeypatch.setattr(deep_analysis, "_general_source_rows", lambda _brand: [])
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


def test_disabled_cache_mode_keeps_stale_brand_elements_read_only(monkeypatch) -> None:
    stale = {
        "리바로": {
            "brand_name": "리바로",
            "strength": {"status": "stale"},
        }
    }
    monkeypatch.setattr(
        deep_analysis,
        "get_settings",
        lambda: SimpleNamespace(cache_write_mode="disabled"),
    )
    monkeypatch.setattr(
        deep_analysis,
        "_load_cached_brand_elements_read_only",
        lambda brand_keys: stale if brand_keys == ["리바로"] else {},
    )
    monkeypatch.setattr(
        deep_analysis,
        "_refresh_cached_brand_elements",
        lambda _brand_keys: pytest.fail("disabled mode must not refresh brand elements"),
    )
    monkeypatch.setattr(
        deep_analysis.db,
        "fetch_all",
        lambda *_args, **_kwargs: pytest.fail("disabled mode must use the read-only loader"),
    )

    assert deep_analysis._load_cached_brand_elements(["리바로"]) == stale


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
    assert "brand_key = %s" in sql
    assert " OR " not in sql
    assert "atc4_code = %s" not in sql
    assert params == ["리바로젯", "iqvia_nsa"]


def test_general_metric_lookup_falls_back_to_name_when_key_rows_miss_requested_market(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        if "brand_key = %s" in sql:
            return [{"atc4_code": "A10B0"}]
        return [{"atc4_code": "C10C0"}]

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    rows = deep_analysis._fetch_general_metric_rows("리바로젯", atc4="C10C0")

    assert rows == [{"atc4_code": "C10C0"}]
    assert len(calls) == 2
    assert "brand_key = %s" in calls[0][0]
    assert "brand_name = %s" in calls[1][0]


def test_general_source_rows_unions_key_and_name_rows_by_atc4_max(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        if "brand_key = %s" in sql:
            return [
                {
                    "atc4_code": "C10C0",
                    "source_computed_at": datetime(2026, 8, 8, tzinfo=deep_analysis.KST),
                },
                {
                    "atc4_code": "A10B0",
                    "source_computed_at": datetime(2026, 8, 9, tzinfo=deep_analysis.KST),
                },
            ]
        return [
            {
                "atc4_code": "C10C0",
                "source_computed_at": datetime(2026, 8, 10, tzinfo=deep_analysis.KST),
            },
            {
                "atc4_code": "D10A0",
                "source_computed_at": datetime(2026, 8, 7, tzinfo=deep_analysis.KST),
            },
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    rows = deep_analysis._general_source_rows("리바로젯")

    assert {row["atc4_code"]: row["source_computed_at"] for row in rows} == {
        "A10B0": datetime(2026, 8, 9, tzinfo=deep_analysis.KST),
        "C10C0": datetime(2026, 8, 10, tzinfo=deep_analysis.KST),
        "D10A0": datetime(2026, 8, 7, tzinfo=deep_analysis.KST),
    }
    assert len(calls) == 2
    assert "brand_key = %s" in calls[0][0]
    assert "brand_name = %s" in calls[1][0]
    assert all(" OR " not in sql for sql, _params in calls)
    assert all(params == ["리바로젯"] for _sql, params in calls)


def test_general_source_rows_empty_key_result_still_uses_name_rows(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        if "brand_name = %s" in sql:
            return [{"atc4_code": "C10C0", "source_computed_at": datetime(2026, 8, 9)}]
        return []

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    assert deep_analysis._general_source_rows("표시명")
    assert len(calls) == 2
    assert "brand_key = %s" in calls[0][0]
    assert "brand_name = %s" in calls[1][0]
    assert all(" OR " not in sql for sql, _params in calls)


def test_general_source_rows_does_not_swallow_db_failure(monkeypatch) -> None:
    def fail_fetch_all(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise deep_analysis.pymysql.OperationalError(2006, "connection lost")

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fail_fetch_all)

    with pytest.raises(deep_analysis.pymysql.OperationalError, match="connection lost"):
        deep_analysis._general_source_rows("리바로젯")


def test_general_cache_miss_without_compact_variant_logs_reason(monkeypatch, caplog) -> None:
    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: None)

    with caplog.at_level(logging.WARNING, logger=deep_analysis.__name__):
        assert deep_analysis._fetch_general_deep_analysis_row("리바로젯") is None

    assert "reason=exact_cache_miss_no_compact_variant" in caplog.text


def test_general_cache_schema_fallback_logs_error_code(monkeypatch, caplog) -> None:
    def fail_fetch_one(*_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        raise deep_analysis.pymysql.ProgrammingError(1146, "table missing")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fail_fetch_one)

    with caplog.at_level(logging.WARNING, logger=deep_analysis.__name__):
        assert deep_analysis._fetch_general_deep_analysis_row("리바로젯") is None

    assert "reason=cache_schema_unavailable" in caplog.text
    assert "error_code=1146" in caplog.text


def test_general_cache_freshness_uses_the_row_raw_code_within_normalized_market(monkeypatch) -> None:
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
        "source_computed_at": datetime(2026, 7, 1),
        "is_stale": 0,
    }

    assert deep_analysis._general_cache_row_fresh(row, "리바로젯", "C10C") is True


def test_general_cache_rejects_a_truly_stale_raw_code(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis.db,
        "fetch_all",
        lambda *_args, **_kwargs: [
            {"atc4_code": "C10C", "source_computed_at": datetime(2026, 7, 2)},
            {"atc4_code": "C10C0", "source_computed_at": datetime(2026, 7, 1)},
        ],
    )
    row = {
        "atc4_code": "C10C",
        "source_computed_at": datetime(2026, 7, 1),
        "is_stale": 0,
    }

    assert deep_analysis._general_cache_row_fresh(row, "리바로젯", "C10C0") is False


def _source_cache_row(
    atc4: str,
    combo: str,
    *,
    brand_count: int,
    history_count: int,
    forecast_count: int,
    is_stale: int = 0,
) -> dict[str, Any]:
    source = combo.split(".", 1)[0]
    brands = [
        {
            "brand": "리바로젯" if index == 0 else f"경쟁{index}",
            "history_values": list(range(history_count)),
            "forecast_values": list(range(forecast_count)),
        }
        for index in range(brand_count)
    ]
    payload = {
        "brand": "리바로젯",
        "brand_name": "리바로젯",
        "brand_key": "리바로젯",
        "market_id": f"general:{atc4}",
        "available_combos": [combo],
        "data": {
            "forecast": {
                "method": "data_size_dispatch_v1_phase30_baseline",
                "by_combo": {combo: {"brands": brands}},
            },
            "simulation": {"by_combo": {combo: {"brands": brands}}},
            "events": [],
        },
        "market_meta": {
            "atc4_code": atc4,
            "sources": [source],
            "available_combos": [combo],
            "default_source": source,
            "source_count": 1,
            "measure_count": 1,
            "market_count": 1,
        },
    }
    return {
        "brand_key": "리바로젯",
        "brand": "리바로젯",
        "response_json": json.dumps(payload, ensure_ascii=False),
        "brand_factors": json.dumps(
            {
                "atc": [atc4],
                "ubist": {"seller": ["UBIST사"] if source == "UBIST" else []},
                "iqvia": {"mfr_name_kor": ["IQVIA사"] if source == "IQVIA" else []},
            },
            ensure_ascii=False,
        ),
        "updated_at": datetime(2026, 7, 1),
        "source_computed_at": datetime(2026, 7, 1),
        "expires_at": None,
        "is_stale": is_stale,
        "stale_reason": "fixture_stale" if is_stale else None,
        "stale_marked_at": datetime(2026, 7, 2) if is_stale else None,
        "atc4_code": atc4,
    }


def test_general_cache_merges_source_native_rows_for_one_normalized_market(monkeypatch) -> None:
    cache_rows = [
        _source_cache_row("C10C", "UBIST.sales", brand_count=6, history_count=65, forecast_count=60),
        _source_cache_row("C10C0", "IQVIA.sales", brand_count=6, history_count=20, forecast_count=20),
    ]

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        if "cache_deep_analysis_general" in sql:
            return cache_rows
        if "mart_general_brand_metric" in sql:
            return [
                {"atc4_code": "C10C", "source_computed_at": datetime(2026, 7, 1)},
                {"atc4_code": "C10C0", "source_computed_at": datetime(2026, 7, 1)},
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: cache_rows[0])
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    row = deep_analysis._fetch_general_deep_analysis_row("리바로젯", "C10C0")

    assert row is not None
    payload = json.loads(row["response_json"])
    assert payload["market_id"] == "general:C10C"
    assert payload["market_meta"]["sources"] == ["IQVIA", "UBIST"]
    assert payload["market_meta"]["available_combos"] == ["IQVIA.sales", "UBIST.sales"]
    assert set(payload["data"]["forecast"]["by_combo"]) == {"IQVIA.sales", "UBIST.sales"}


def test_general_cache_value_gate_restores_competitors_and_forecast_points(monkeypatch) -> None:
    cache_rows = [
        _source_cache_row("C10C", "UBIST.sales", brand_count=6, history_count=65, forecast_count=60),
        _source_cache_row("C10C0", "IQVIA.sales", brand_count=6, history_count=20, forecast_count=20),
    ]

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        if "cache_deep_analysis_general" in sql:
            return cache_rows
        return [
            {"atc4_code": "C10C", "source_computed_at": datetime(2026, 7, 1)},
            {"atc4_code": "C10C0", "source_computed_at": datetime(2026, 7, 1)},
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: cache_rows[1])
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    row = deep_analysis._fetch_general_deep_analysis_row("리바로젯", "C10C0")

    assert row is not None
    by_combo = json.loads(row["response_json"])["data"]["forecast"]["by_combo"]
    assert len(by_combo["UBIST.sales"]["brands"]) == 6
    assert {len(brand["history_values"]) for brand in by_combo["UBIST.sales"]["brands"]} == {65}
    assert {len(brand["forecast_values"]) for brand in by_combo["UBIST.sales"]["brands"]} == {60}
    assert len(by_combo["IQVIA.sales"]["brands"]) == 6
    assert {len(brand["history_values"]) for brand in by_combo["IQVIA.sales"]["brands"]} == {20}
    assert {len(brand["forecast_values"]) for brand in by_combo["IQVIA.sales"]["brands"]} == {20}


def test_general_cache_rejects_the_normalized_group_when_any_source_row_is_stale(monkeypatch) -> None:
    cache_rows = [
        _source_cache_row("C10C", "UBIST.sales", brand_count=6, history_count=65, forecast_count=60),
        _source_cache_row("C10C0", "IQVIA.sales", brand_count=6, history_count=20, forecast_count=20),
    ]

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        if "cache_deep_analysis_general" in sql:
            return cache_rows
        return [
            {"atc4_code": "C10C", "source_computed_at": datetime(2026, 7, 1)},
            {"atc4_code": "C10C0", "source_computed_at": datetime(2026, 7, 2)},
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: cache_rows[1])
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    assert deep_analysis._fetch_general_deep_analysis_row("리바로젯", "C10C0") is None


def test_general_cache_stale_group_logs_fallback_reason(monkeypatch, caplog) -> None:
    cache_rows = [
        _source_cache_row("C10C", "UBIST.sales", brand_count=6, history_count=65, forecast_count=60),
        _source_cache_row("C10C0", "IQVIA.sales", brand_count=6, history_count=20, forecast_count=20),
    ]

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        if "cache_deep_analysis_general" in sql:
            return cache_rows
        return [
            {"atc4_code": "C10C", "source_computed_at": datetime(2026, 7, 1)},
            {"atc4_code": "C10C0", "source_computed_at": datetime(2026, 7, 2)},
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", lambda *_args, **_kwargs: cache_rows[1])
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    with caplog.at_level(logging.WARNING, logger=deep_analysis.__name__):
        assert deep_analysis._fetch_general_deep_analysis_row("리바로젯", "C10C0") is None

    assert "reason=stale_source_group" in caplog.text


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


def test_general_mart_fallback_emits_target_plus_top_five_and_degraded(monkeypatch) -> None:
    target = {
        "brand_key": "target",
        "brand_name": "선택",
        "atc4_code": "C10C0",
        "atc4_desc": "지질",
        "source": "iqvia_nsa",
        "measure": "sales",
        "metric_history": json.dumps({"2026-Q1": {"raw_value": 10, "ms": 1.0}}, ensure_ascii=False),
        "unit_label": "원",
        "computed_at": datetime(2026, 8, 9),
    }
    competitors = [
        {
            **target,
            "brand_key": f"c{index}",
            "brand_name": f"경쟁{index}",
            "metric_history": json.dumps(
                {"2026-Q1": {"raw_value": value, "ms": float(index)}},
                ensure_ascii=False,
            ),
        }
        for index, value in enumerate((20, 70, 40, 60, 30, 50), start=1)
    ]
    market_calls: list[tuple[str, str | None]] = []

    def fake_market_rows(atc4: str, *, source: str | None = None) -> list[dict[str, Any]]:
        market_calls.append((atc4, source))
        return [target, *competitors]

    monkeypatch.setattr(deep_analysis, "_fetch_general_metric_rows", lambda *_args, **_kwargs: [target])
    monkeypatch.setattr(deep_analysis, "_fetch_general_market_rows", fake_market_rows)

    result = deep_analysis._general_row_from_mart("선택")

    assert result is not None
    payload = json.loads(result["response_json"])
    brands = payload["data"]["forecast"]["by_combo"]["IQVIA.sales"]["brands"]
    assert [item["brand"] for item in brands] == ["선택", "경쟁2", "경쟁4", "경쟁6", "경쟁3", "경쟁5"]
    assert [item["is_target"] for item in brands] == [True, False, False, False, False, False]
    assert market_calls == [("C10C0", None)]
    assert payload["degraded"] is True
    assert payload["degraded_reason"] == "forecast_block_unavailable"
    assert payload["data"]["forecast"]["degraded"] is True
    assert payload["data"]["forecast"]["degraded_reason"] == "forecast_block_unavailable"
    assert payload["market_meta"]["degraded"] is True
    assert payload["market_meta"]["degraded_reason"] == "forecast_block_unavailable"


def test_general_cache_hit_does_not_add_degraded_key(monkeypatch) -> None:
    row = _row("general", atc4="C10C0")
    expected = json.loads(row["response_json"])
    expected["market_meta"]["is_jw"] = False
    monkeypatch.setattr(deep_analysis, "_fetch_general_deep_analysis_row", lambda _brand: row)
    monkeypatch.setattr(deep_analysis, "_strategic_brand_flags", lambda _brand: (False, False))

    payload, _resolved_row = deep_analysis._compose_general_view_payload("멀티브랜드")

    assert payload == expected
    assert "degraded" not in payload


def test_general_mart_fallback_uses_existing_forecast_blocks(monkeypatch) -> None:
    mart_row = {
        "brand": "리바로",
        "brand_key": "리바로",
        "atc4_code": "C10A1",
        "updated_at": datetime(2026, 8, 10),
        "response_json": json.dumps(
            {
                "degraded": True,
                "degraded_reason": "forecast_block_unavailable",
                "data": {
                    "forecast": {"degraded": True, "by_combo": {}},
                    "simulation": {"by_combo": {}},
                },
                "market_meta": {
                    "sources": ["UBIST", "IQVIA"],
                    "degraded": True,
                    "degraded_reason": "forecast_block_unavailable",
                },
            },
            ensure_ascii=False,
        ),
    }
    ubist = ForecastBlock(
        forecast={"by_combo": {"UBIST.sales": {"forecast_periods": ["2026-06"]}}},
        simulation={"by_combo": {"UBIST.sales": {"available": True}}},
        generation_status="generated",
        no_history_fallback=None,
    )
    iqvia = ForecastBlock(
        forecast={"by_combo": {"IQVIA.sales": {"forecast_periods": ["2026-Q1"]}}},
        simulation={"by_combo": {"IQVIA.sales": {"available": True}}},
        generation_status="generated",
        no_history_fallback=None,
    )
    monkeypatch.setattr(deep_analysis, "_fetch_general_deep_analysis_row", lambda _brand: None)
    monkeypatch.setattr(deep_analysis, "_general_row_from_mart", lambda *_args, **_kwargs: mart_row)
    monkeypatch.setattr(
        deep_analysis,
        "load_forecast_block_by_key",
        lambda **kwargs: ubist if kwargs["source"] == "ubist" else iqvia,
    )

    payload, _ = deep_analysis._compose_general_view_payload("리바로")

    assert set(payload["data"]["forecast"]["by_combo"]) == {
        "UBIST.sales",
        "IQVIA.sales",
    }
    assert payload.get("degraded") is not True
    assert payload["market_meta"].get("degraded") is not True


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
    monkeypatch.setattr(deep_analysis, "_fetch_general_market_rows", lambda *_args, **_kwargs: rows)

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
    monkeypatch.setattr(deep_analysis, "_fetch_general_market_rows", lambda *_args, **_kwargs: rows)

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
    monkeypatch.setattr(deep_analysis, "_fetch_general_market_rows", lambda *_args, **_kwargs: rows)

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
