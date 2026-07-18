from __future__ import annotations

import json
from datetime import datetime
import pytest
from pathlib import Path
import sys
from typing import Any

import pymysql


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.routes import deep_analysis


@pytest.fixture(autouse=True)
def _strategic_mart_seam(monkeypatch):
    monkeypatch.setattr(
        deep_analysis,
        "_strategic_row_from_mart",
        lambda brand: deep_analysis.db.fetch_one("SELECT strategic_test_row WHERE brand = %s", [brand]),
    )
    monkeypatch.setattr(deep_analysis, "_load_deep_events", lambda _brand: [])


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


def _generated_ai_payload(variant: str) -> dict[str, Any]:
    return {
        "analysis_variant": variant,
        "phenomenon": {"title": "현상", "body": "본문", "bullets": [], "evidence": []},
        "cause": {"title": "원인", "body": "본문", "bullets": [], "evidence": []},
        "prediction": {"title": "예측", "body": "본문", "bullets": [], "evidence": []},
        "recommendation": {"title": "권고", "body": "본문", "bullets": [], "evidence": []},
        "evidence_pool": [
            {"news_id": "n1", "title": "뉴스", "published_date": "2026-07-01", "score": 80},
            "kept-non-dict-entry",
        ],
    }


def _strength_row() -> dict[str, Any]:
    return {
        "strength_summary_json": json.dumps(
            {"profile_display": {"headline": "strong"}, "strength_items": [], "limitations": []},
            ensure_ascii=False,
        ),
        "generated_at": "2026-07-05 13:32:16",
        "workflow_rev": 5365,
    }


def _selected_strength_available(payload: dict[str, Any]) -> bool:
    items = payload["data"]["brand_factors"]["iqvia"]
    return bool(items and items[0].get("strength"))


def test_deep_analysis_ai_variants_are_not_generated_when_row_absent(monkeypatch) -> None:
    # Given: the AI analysis table has no row for the requested brand.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return None
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: short/long keys are present and unavailable without changing legacy sections.
    assert payload["data"]["ai_analysis"] == {}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert _selected_strength_available(payload) is False


def test_deep_analysis_ai_variants_remove_generation_only_fields(monkeypatch) -> None:
    # Given: short/long cache columns still include generation-only metadata.
    base_analysis = _generated_ai_payload("base")
    ai_row = {
        "ai_analysis_json": json.dumps(base_analysis, ensure_ascii=False),
        "ai_analysis_short_json": json.dumps(_generated_ai_payload("short"), ensure_ascii=False),
        "ai_analysis_long_json": json.dumps(_generated_ai_payload("long"), ensure_ascii=False),
    }

    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return ai_row
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is served.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: only short/long are normalized; legacy ai_analysis remains untouched.
    assert payload["data"]["ai_analysis"] == base_analysis
    for key in ("ai_analysis_short", "ai_analysis_long"):
        variant_payload = payload["data"][key]
        assert "analysis_variant" not in variant_payload
        assert "published_date" not in variant_payload["evidence_pool"][0]
        assert variant_payload["evidence_pool"][1] == "kept-non-dict-entry"


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
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: malformed variant content cannot break the main response.
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert _selected_strength_available(payload) is False


def test_formal_ai_variants_select_canonical_lineage_without_inventing_time(monkeypatch) -> None:
    rows = [
        {
            "brand": "리바로",
            "ai_analysis_short_json": json.dumps({"headline": "legacy"}),
            "short_generation_status": "legacy_unbound",
            "short_generated_at": None,
            "ai_analysis_long_json": json.dumps({"headline": "fallback"}),
            "long_generation_status": "complete_template_fallback",
            "long_generated_at": datetime(2026, 7, 12, 1, 2, 3),
        },
        {
            "brand": "리바로",
            "ai_analysis_short_json": json.dumps({"headline": "complete"}),
            "short_generation_status": "complete",
            "short_generated_at": datetime(2026, 7, 13, 4, 5, 6),
            "ai_analysis_long_json": json.dumps({"headline": "legacy"}),
            "long_generation_status": "legacy_unbound",
            "long_generated_at": None,
        },
    ]
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: rows)

    short, long = deep_analysis._load_canonical_ai_analysis_variants("리바로")

    assert short == {
        "headline": "complete",
        "generation_status": "complete",
        "generated_at": "2026-07-13T04:05:06+09:00",
    }
    assert long == {
        "headline": "fallback",
        "generation_status": "complete_template_fallback",
        "generated_at": "2026-07-12T01:02:03+09:00",
    }


def test_formal_ai_variant_missing_origin_time_is_explicitly_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        deep_analysis.db,
        "fetch_all",
        lambda *_args, **_kwargs: [
            {
                "brand": "니코브렉",
                "ai_analysis_short_json": json.dumps({"headline": "legacy"}),
                "short_generation_status": "legacy_unbound",
                "short_generated_at": None,
                "ai_analysis_long_json": json.dumps({"headline": "legacy"}),
                "long_generation_status": "legacy_unbound",
                "long_generated_at": "not-a-timestamp",
            }
        ],
    )

    short, long = deep_analysis._load_canonical_ai_analysis_variants("니코브렉")

    for variant in (short, long):
        assert variant["generation_status"] == "legacy_unbound"
        assert variant["generated_at"] is None
        assert variant["timestamp_status"] == "unknown"


def test_formal_ai_variants_fall_back_when_lineage_columns_are_absent(monkeypatch) -> None:
    def missing_lineage(*_args, **_kwargs):
        raise pymysql.err.ProgrammingError(1054, "Unknown column 'short_generated_at'")

    monkeypatch.setattr(deep_analysis.db, "fetch_all", missing_lineage)
    monkeypatch.setattr(
        deep_analysis.db,
        "fetch_one",
        lambda *_args, **_kwargs: {
            "brand": "구환경브랜드",
            "ai_analysis_short_json": json.dumps({"headline": "short"}),
            "ai_analysis_long_json": json.dumps({"headline": "long"}),
        },
    )

    short, long = deep_analysis._load_canonical_ai_analysis_variants("구환경브랜드")

    assert short["headline"] == "short"
    assert long["headline"] == "long"
    for variant in (short, long):
        assert variant["generation_status"] == "unknown"
        assert variant["generated_at"] is None
        assert variant["timestamp_status"] == "unknown"


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
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: the AI lookup failure cannot make the route 5xx.
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert _selected_strength_available(payload) is False


def test_deep_analysis_legacy_ai_analysis_unchanged_when_variant_columns_absent(monkeypatch) -> None:
    # Given: an older AI row shape with only the legacy column.
    def fake_fetch_one(sql: str, _params: list[str]) -> dict[str, Any] | None:
        if "cache_deep_analysis_ai_analysis" in sql:
            return _legacy_ai_row()
        if "agent3_brand_strength" in sql:
            return _strength_row()
        return _cache_row()

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(deep_analysis.db, "fetch_all", lambda *_args, **_kwargs: [])

    # When: deep-analysis is requested.
    payload = deep_analysis.deep_analysis("리바로")

    # Then: legacy ai_analysis remains pass-through while new siblings degrade independently.
    assert payload["data"]["ai_analysis"] == {"summary": "ok"}
    assert payload["data"]["ai_analysis_short"] == {"available": False, "reason": "not_generated"}
    assert payload["data"]["ai_analysis_long"] == {"available": False, "reason": "not_generated"}
    assert _selected_strength_available(payload) is False
