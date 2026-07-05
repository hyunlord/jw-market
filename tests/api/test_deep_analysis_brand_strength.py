from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pymysql


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.routes import deep_analysis


def _series(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-{index:03d}" for index in range(count)]


def _cache_row() -> dict[str, Any]:
    return {
        "response_json": json.dumps(
            {
                "brand": "리바로",
                "data": {"forecast": {}, "existing": {"value": 1}},
            },
            ensure_ascii=False,
        ),
        "updated_at": "2026-07-05T12:00:00+09:00",
    }


def _ai_row() -> dict[str, str]:
    return {
        "ai_analysis_json": json.dumps({"summary": "ok"}, ensure_ascii=False),
        "ai_analysis_short_json": json.dumps(
            {"analysis_variant": "short", "evidence_pool": [{"source": "뉴스"}]},
            ensure_ascii=False,
        ),
        "ai_analysis_long_json": json.dumps(
            {"analysis_variant": "long", "evidence_pool": [{"source": "뉴스"}]},
            ensure_ascii=False,
        ),
    }


def _strength_row() -> dict[str, Any]:
    return {
        "strength_summary_json": json.dumps(
            {
                "brand": "리바로",
                "profile_display": {"headline": "strong"},
                "strength_items": [{"axis": "growth", "score": 1}],
                "limitations": ["pilot"],
            },
            ensure_ascii=False,
        ),
        "generated_at": "2026-07-05 13:32:16",
        "workflow_rev": 5365,
    }


def test_slice_forecast_horizon_keeps_five_year_monthly_prefix_and_slices_all_intervals() -> None:
    # Given: a monthly forecast with more than five years and an interval key unknown to older code.
    payload = {
        "data": {
            "forecast": {
                "by_combo": {
                    "UBIST.sales": {
                        "period_unit": "월",
                        "forecast_periods": _series("m", 121),
                        "forecast_values": _series("value", 121),
                        "forecast_ms_pct": _series("ms", 121),
                        "forecast_intervals": {
                            "upper_horizon_adaptive": _series("upper", 121),
                            "custom_interval_from_cache": _series("custom", 121),
                            "metadata": {"kept": True},
                        },
                        "brands": [
                            {
                                "brand": "리바로",
                                "forecast_values": _series("brand-value", 121),
                                "forecast_ms_pct": _series("brand-ms", 121),
                                "forecast_intervals": {
                                    "ci_upper_95": _series("brand-upper", 121),
                                    "custom_brand_interval": _series("brand-custom", 121),
                                    "lower_floor_applied": False,
                                },
                            }
                        ],
                    }
                }
            },
            "existing": {"untouched": True},
        }
    }

    # When: the route horizon slicer is applied.
    deep_analysis._slice_forecast_horizon(payload)

    # Then: every forecast list is a five-year monthly prefix and non-forecast data is untouched.
    combo = payload["data"]["forecast"]["by_combo"]["UBIST.sales"]
    assert combo["forecast_periods"] == _series("m", 60)
    assert combo["forecast_values"] == _series("value", 60)
    assert combo["forecast_ms_pct"] == _series("ms", 60)
    assert combo["forecast_intervals"]["upper_horizon_adaptive"] == _series("upper", 60)
    assert combo["forecast_intervals"]["custom_interval_from_cache"] == _series("custom", 60)
    assert combo["forecast_intervals"]["metadata"] == {"kept": True}
    brand = combo["brands"][0]
    assert brand["forecast_values"] == _series("brand-value", 60)
    assert brand["forecast_ms_pct"] == _series("brand-ms", 60)
    assert brand["forecast_intervals"]["ci_upper_95"] == _series("brand-upper", 60)
    assert brand["forecast_intervals"]["custom_brand_interval"] == _series("brand-custom", 60)
    assert brand["forecast_intervals"]["lower_floor_applied"] is False
    assert payload["data"]["existing"] == {"untouched": True}


def test_slice_forecast_horizon_keeps_five_year_quarterly_prefix() -> None:
    # Given: a quarterly forecast with ten years of values.
    payload = {
        "data": {
            "forecast": {
                "by_combo": {
                    "IQVIA.sales": {
                        "period_unit": "분기",
                        "forecast_periods": _series("q", 40),
                        "forecast_intervals": {"upper_95_natural": _series("upper-q", 40)},
                        "brands": [
                            {
                                "brand": "가드렛",
                                "forecast_values": _series("brand-q", 40),
                                "forecast_intervals": {"lower_95_natural": _series("lower-q", 40)},
                            }
                        ],
                    }
                }
            }
        }
    }

    # When: the route horizon slicer is applied.
    deep_analysis._slice_forecast_horizon(payload)

    # Then: every forecast list is a five-year quarterly prefix.
    combo = payload["data"]["forecast"]["by_combo"]["IQVIA.sales"]
    assert combo["forecast_periods"] == _series("q", 20)
    assert combo["forecast_intervals"]["upper_95_natural"] == _series("upper-q", 20)
    brand = combo["brands"][0]
    assert brand["forecast_values"] == _series("brand-q", 20)
    assert brand["forecast_intervals"]["lower_95_natural"] == _series("lower-q", 20)


def test_deep_analysis_injects_brand_strength_when_agent3_row_exists(monkeypatch) -> None:
    # Given: cache rows and an Agent3 strength row for the requested brand.
    queries: list[str] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        queries.append(sql)
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            assert "WHERE serving_brand_name = %s" in sql
            assert "WHERE brand_name = %s" not in sql
            assert params == ["리바로"]
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: the portal deep-analysis route composes the cached payload.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: the existing payload remains and brand_strength is a pass-through summary.
    assert payload["data"]["existing"] == {"value": 1}
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {
        "analysis_variant": "short",
        "evidence_pool": [{"source": "뉴스"}],
    }
    assert payload["data"]["ai_analysis_long"] == {
        "analysis_variant": "long",
        "evidence_pool": [{"source": "뉴스"}],
    }
    assert payload["data"]["brand_strength"] == {
        "available": True,
        "profile_display": {"headline": "strong"},
        "strength_items": [{"axis": "growth", "score": 1}],
        "limitations": ["pilot"],
        "meta": {"generated_at": "2026-07-05 13:32:16", "workflow_rev": 5365},
    }
    assert "response_json" not in json.dumps(payload["data"]["brand_strength"], ensure_ascii=False)


def test_deep_analysis_brand_strength_is_not_generated_when_row_absent(monkeypatch) -> None:
    # Given: the Agent3 table has no row for the brand.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            return None
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: the key is still present and uses the unavailable contract.
    assert payload["data"]["brand_strength"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_short"]["analysis_variant"] == "short"
    assert payload["data"]["ai_analysis_long"]["analysis_variant"] == "long"


def test_deep_analysis_brand_strength_handles_invalid_json(monkeypatch) -> None:
    # Given: Agent3 returns malformed JSON.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            return {"strength_summary_json": "not-json", "generated_at": None, "workflow_rev": None}
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: malformed Agent3 content cannot break the main response.
    assert payload["data"]["brand_strength"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_short"]["analysis_variant"] == "short"
    assert payload["data"]["ai_analysis_long"]["analysis_variant"] == "long"


def test_deep_analysis_brand_strength_handles_db_failure(monkeypatch) -> None:
    # Given: the Agent3 lookup fails at the database boundary.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            raise pymysql.err.OperationalError(1142, "denied")
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: DB failure degrades only the new section.
    assert payload["data"]["brand_strength"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_short"]["analysis_variant"] == "short"
    assert payload["data"]["ai_analysis_long"]["analysis_variant"] == "long"


def test_deep_analysis_strip_brand_strength_matches_previous_payload(monkeypatch) -> None:
    # Given: the previous response plus the new Agent3 section.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: callers strip the newly added field.
    payload = deep_analysis.deep_analysis("리바로")
    payload["data"].pop("brand_strength")
    payload["data"].pop("ai_analysis_short")
    payload["data"].pop("ai_analysis_long")

    # Then: the original deep-analysis contract is unchanged.
    assert payload["brand"] == "리바로"
    assert payload["data"] == {"forecast": {}, "existing": {"value": 1}, "ai_analysis": {"summary": "ok"}}
