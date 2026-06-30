from __future__ import annotations

import json

import pymysql

from pipeline.scripts.api.routes import deep_analysis


def test_load_ai_analysis_variants_returns_sibling_keys(monkeypatch):
    def fake_fetch_one(sql, params):
        assert "ai_analysis_short_json" in sql
        assert params == ["헴리브라"]
        return {
            "ai_analysis_short_json": json.dumps({"prediction": {"title": "단기"}}),
            "ai_analysis_long_json": json.dumps({"prediction": {"title": "장기"}}),
        }

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    payload = deep_analysis._load_ai_analysis_variants("헴리브라")

    assert payload == {
        "ai_analysis_short": {"prediction": {"title": "단기"}},
        "ai_analysis_long": {"prediction": {"title": "장기"}},
    }


def test_load_ai_analysis_variants_is_compatible_before_columns_exist(monkeypatch):
    def fake_fetch_one(sql, params):
        raise pymysql.err.ProgrammingError(1054, "Unknown column 'ai_analysis_short_json'")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    assert deep_analysis._load_ai_analysis_variants("헴리브라") == {}


def test_load_ai_analysis_variants_handles_operational_error_before_columns_exist(monkeypatch):
    def fake_fetch_one(sql, params):
        raise pymysql.err.OperationalError(1054, "Unknown column 'ai_analysis_short_json'")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    assert deep_analysis._load_ai_analysis_variants("헴리브라") == {}


def test_load_ai_analysis_variants_keeps_other_operational_errors_loud(monkeypatch):
    def fake_fetch_one(sql, params):
        raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")

    monkeypatch.setattr(deep_analysis.db, "fetch_one", fake_fetch_one)

    try:
        deep_analysis._load_ai_analysis_variants("헴리브라")
    except pymysql.err.OperationalError as exc:
        assert exc.args[0] == 2003
    else:
        raise AssertionError("Expected non-schema OperationalError to propagate")
