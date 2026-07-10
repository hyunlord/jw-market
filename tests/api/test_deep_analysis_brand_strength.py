from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pymysql
from fastapi import HTTPException


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


def _source_strength_row(source: str, *, brand_key: str = "리바로", serving_brand_name: str = "리바로") -> dict[str, Any]:
    return {
        "brand_key": brand_key,
        "serving_brand_name": serving_brand_name,
        "source": source,
        "strength_summary_json": json.dumps(
            {
                "brand": brand_key,
                "profile_display": {"headline": f"{source} strong"},
                "strength_items": [{"axis": source}],
                "limitations": [] if source == "iqvia" else [f"{source} candidate 0건"],
                "workflow_id": 99,
                "input_hash": "hidden",
            },
            ensure_ascii=False,
        ),
    }


def _selected_overall(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload["data"]["brand_factors"]["iqvia"]
    return items[0].get("strength", {}) if items else {}


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


def test_deep_analysis_uses_only_source_level_brand_strength(monkeypatch) -> None:
    # Given: cache rows and an Agent3 source-level strength row for the requested brand.
    queries: list[str] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        queries.append(sql)
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        assert "agent3_brand_strength" not in sql
        return _cache_row()

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        queries.append(sql)
        if "cache_brand_elements" in sql:
            return []
        if "brand_key IN" in sql:
            return [_source_strength_row("iqvia")]
        if "agent3_brand_strength_source" in sql:
            return []
        raise AssertionError(sql)

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When: the portal deep-analysis route composes the cached payload.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: the existing payload remains and only source-level strength is exposed.
    assert payload["data"]["existing"] == {"value": 1}
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"evidence_pool": [{"source": "뉴스"}]}
    assert payload["data"]["ai_analysis_long"] == {"evidence_pool": [{"source": "뉴스"}]}
    assert payload["data"]["brand_factors"]["iqvia"][0] == {
        "brand": "리바로",
        "brand_key": "리바로",
        "role": "selected",
        "rank": None,
        "factors": {
            "available": False,
            "reason": "not_generated",
            "values": {
                "mfr_name_kor": [],
                "molecule_type": [],
                "molecule_desc": [],
                "pack_desc": [],
                "strength": [],
                "nhi_type": [],
            },
        },
        "strength": {
            "profile_display": {"headline": "iqvia strong"},
            "strength_items": [{"axis": "iqvia"}],
            "limitations": [],
        },
    }
    assert payload["data"]["brand_factors"]["ubist"] == []
    serialized = json.dumps(payload["data"]["brand_factors"]["iqvia"][0], ensure_ascii=False)
    assert "response_json" not in serialized
    assert "workflow_id" not in serialized
    assert not any("FROM `jw_mart_d2_stage_20260630_r2`.agent3_brand_strength\n" in sql for sql in queries)


def test_deep_analysis_cache_lookup_falls_back_to_compact_brand_and_uses_matched_brand(monkeypatch) -> None:
    # Given: the URL has a display-space variant, while cache and Agent3 rows use the compact brand.
    seen: list[tuple[str, list[str]]] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        seen.append((sql, params))
        if "cache_deep_analysis_ai_analysis" in sql:
            assert params == ["리바로브이"]
            return _ai_row()
        assert "agent3_brand_strength" not in sql
        if "cache_deep_analysis" in sql:
            assert params == ["리바로 브이"]
            return None
        raise AssertionError(sql)

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        seen.append((sql, params))
        if "cache_brand_elements" in sql:
            return []
        if "agent3_brand_strength_source" in sql:
            return []
        assert "REPLACE" in sql
        assert params == ["리바로브이"]
        return [{**_cache_row(), "brand": "리바로브이"}]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When: the spaced display variant is requested.
    payload = deep_analysis.deep_analysis("리바로 브이")

    # Then: the compact fallback serves the cache and downstream lookups use the matched canonical brand.
    assert payload["data"]["brand_factors"] == {"iqvia": [], "ubist": []}
    assert any("cache_brand_elements" in sql and params == ["리바로브이"] for sql, params in seen)
    assert any("cache_deep_analysis" in sql and "REPLACE" in sql for sql, _params in seen)


def test_deep_analysis_compact_cache_lookup_rejects_ambiguous_matches(monkeypatch) -> None:
    # Given: compact fallback would match multiple stored brand labels.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis" in sql:
            return None
        return _ai_row()

    def fake_fetch_all(sql: str, _params: list[str]) -> list[dict[str, Any]]:
        if "cache_brand_elements" in sql:
            return []
        if "agent3_brand_strength_source" in sql:
            return []
        assert "REPLACE" in sql
        return [
            {**_cache_row(), "brand": "AB C"},
            {**_cache_row(), "brand": "A BC"},
        ]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When / Then: ambiguous compact-only cache hits are not guessed.
    try:
        deep_analysis.deep_analysis("ABC")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected 404")


def test_load_brand_strength_falls_back_to_compact_serving_brand(monkeypatch) -> None:
    # Given: Agent3 exact lookup misses, but one compact serving_brand_name row exists.
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        calls.append((sql, params))
        assert "serving_brand_name = %s" in sql
        return None

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        assert "REPLACE" in sql
        assert params == ["리바로브이"]
        return [_strength_row()]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When: the display-space variant is looked up.
    strength = deep_analysis._load_brand_strength("리바로 브이")

    # Then: the compact fallback returns the Agent3 strength row.
    assert strength["available"] is True
    assert len(calls) == 2


def test_load_brand_strength_by_source_projects_exact_rows(monkeypatch) -> None:
    # Given: Agent3 source rows exist for both source values under the same brand key.
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        assert "brand_key IN (%s)" in sql
        assert "workflow_id" not in sql
        return [_source_strength_row("iqvia"), _source_strength_row("ubist")]

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When: source-level strength is loaded for the six-slot brand set.
    by_brand = deep_analysis._load_brand_strength_by_source(["리바로"])

    # Then: only API-safe summary fields are exposed per source.
    assert by_brand == {
        "리바로": {
            "iqvia": {"profile_display": {"headline": "iqvia strong"}, "strength_items": [{"axis": "iqvia"}], "limitations": []},
            "ubist": {
                "profile_display": {"headline": "ubist strong"},
                "strength_items": [{"axis": "ubist"}],
                "limitations": ["ubist candidate 0건"],
            },
        }
    }
    assert len(calls) == 1


def test_load_brand_strength_by_source_uses_compact_serving_brand_for_missing_source(monkeypatch) -> None:
    # Given: exact brand_key lookup has only IQVIA, while UBIST uses a spaced serving name.
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        if "brand_key IN" in sql:
            return [_source_strength_row("iqvia", brand_key="리바로브이", serving_brand_name="리바로브이")]
        assert "REPLACE" in sql
        assert params == ["리바로브이", "iqvia", "ubist"]
        return [_source_strength_row("ubist", brand_key="리바로 브이", serving_brand_name="리바로 브이")]

    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When: the requested key is compact but the source row serving name is spaced.
    by_brand = deep_analysis._load_brand_strength_by_source(["리바로브이"])

    # Then: exact rows win, and compact fallback fills only the missing source.
    assert sorted(by_brand["리바로브이"]) == ["iqvia", "ubist"]
    assert by_brand["리바로브이"]["ubist"]["limitations"] == ["ubist candidate 0건"]
    assert len(calls) == 2


def test_load_brand_strength_by_source_skips_absent_brand(monkeypatch) -> None:
    # Given: the source-level table has no exact or compact rows for the brand.
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: a non-buildable brand is requested.
    by_brand = deep_analysis._load_brand_strength_by_source(["미생성브랜드"])

    # Then: absence is represented as an empty mapping, not an error.
    assert by_brand == {}


def test_load_ai_analysis_variants_fall_back_to_compact_brand(monkeypatch) -> None:
    # Given: AI variant exact lookup misses but compact lookup has one canonical brand row.
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch_one(sql: str, params: list[str]) -> dict[str, Any] | None:
        calls.append((sql, params))
        assert "brand = %s" in sql
        return None

    def fake_fetch_all(sql: str, params: list[str]) -> list[dict[str, Any]]:
        calls.append((sql, params))
        assert "REPLACE" in sql
        assert params == ["리바로브이"]
        return [{**_ai_row(), "brand": "리바로브이"}]

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", fake_fetch_all)

    # When: the display-space variant is loaded from AI analysis cache.
    ai_analysis = deep_analysis._load_ai_analysis("리바로 브이")
    short, long = deep_analysis._load_ai_analysis_variants("리바로 브이")

    # Then: compact fallback serves both AI analysis shapes.
    assert ai_analysis == {"summary": "ok"}
    assert short == {"evidence_pool": [{"source": "뉴스"}]}
    assert long == {"evidence_pool": [{"source": "뉴스"}]}
    assert len(calls) == 4


def test_deep_analysis_brand_strength_is_not_generated_when_row_absent(monkeypatch) -> None:
    # Given: the Agent3 table has no row for the brand.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            return None
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: the key is still present and uses the unavailable contract.
    assert _selected_overall(payload) == {}
    assert "analysis_variant" not in payload["data"]["ai_analysis_short"]
    assert "analysis_variant" not in payload["data"]["ai_analysis_long"]


def test_deep_analysis_brand_strength_handles_invalid_json(monkeypatch) -> None:
    # Given: Agent3 returns malformed JSON.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            return {"strength_summary_json": "not-json", "generated_at": None, "workflow_rev": None}
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: malformed Agent3 content cannot break the main response.
    assert _selected_overall(payload) == {}
    assert "analysis_variant" not in payload["data"]["ai_analysis_short"]
    assert "analysis_variant" not in payload["data"]["ai_analysis_long"]


def test_deep_analysis_brand_strength_handles_db_failure(monkeypatch) -> None:
    # Given: the Agent3 lookup fails at the database boundary.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            raise pymysql.err.OperationalError(1142, "denied")
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: DB failure degrades only the new section.
    assert _selected_overall(payload) == {}
    assert "analysis_variant" not in payload["data"]["ai_analysis_short"]
    assert "analysis_variant" not in payload["data"]["ai_analysis_long"]


def test_deep_analysis_strip_brand_strength_matches_previous_payload(monkeypatch) -> None:
    # Given: the previous response plus the new Agent3 section.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _ai_row()
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: callers strip the newly added field.
    payload = deep_analysis.deep_analysis("리바로")
    payload["data"].pop("brand_factors")
    payload["data"].pop("ai_analysis_short")
    payload["data"].pop("ai_analysis_long")

    # Then: the original deep-analysis contract is unchanged.
    assert payload["brand"] == "리바로"
    assert payload["data"] == {"forecast": {}, "existing": {"value": 1}, "ai_analysis": {"summary": "ok"}}
