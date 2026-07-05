from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pymysql


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.routes import deep_analysis


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


def _legacy_ai_row() -> dict[str, str]:
    return {"ai_analysis_json": json.dumps({"summary": "ok"}, ensure_ascii=False)}


def _strength_row() -> dict[str, Any]:
    return {
        "strength_summary_json": json.dumps(
            {"profile_display": {"headline": "strong"}, "strength_items": [], "limitations": []},
            ensure_ascii=False,
        ),
        "generated_at": "2026-07-05 13:32:16",
        "workflow_rev": 5365,
    }


def test_deep_analysis_ai_variants_are_not_generated_when_row_absent(monkeypatch) -> None:
    # Given: the AI analysis table has no row for the requested brand.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return None
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: short/long keys are present and unavailable without changing legacy sections.
    assert payload["data"]["ai_analysis"] == {}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["brand_strength"]["available"] is True


def test_deep_analysis_ai_variants_handle_invalid_json(monkeypatch) -> None:
    # Given: short/long JSON exists but is malformed or not an object.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return {
                "ai_analysis_json": json.dumps({"summary": "ok"}, ensure_ascii=False),
                "ai_analysis_short_json": "not-json",
                "ai_analysis_long_json": json.dumps(["not", "object"], ensure_ascii=False),
            }
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: malformed variant content cannot break the main response.
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["brand_strength"]["available"] is True


def test_deep_analysis_ai_variants_handle_db_failure(monkeypatch) -> None:
    # Given: the AI variant lookup fails at the database boundary.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "ai_analysis_short_json" in sql:
            raise pymysql.err.OperationalError(1142, "denied")
        if "cache_deep_analysis_ai_analysis" in sql:
            return _legacy_ai_row()
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: the AI lookup failure cannot make the route 5xx.
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["brand_strength"]["available"] is True


def test_deep_analysis_legacy_ai_analysis_unchanged_when_variant_columns_absent(monkeypatch) -> None:
    # Given: an older AI row shape with only the legacy column.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _legacy_ai_row()
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: legacy ai_analysis remains pass-through while new siblings degrade independently.
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["brand_strength"]["available"] is True
