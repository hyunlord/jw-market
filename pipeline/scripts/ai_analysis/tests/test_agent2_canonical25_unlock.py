from __future__ import annotations

import stage3a7_create_and_insert_ai_analysis as stage3a7
from bundle_builder import event_bundle_builder


class RecordingCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append((sql, tuple(params or ())))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def fetchall(self):
        return self.rows

    def fetchone(self):
        if "SHOW COLUMNS FROM zeta_analysis_runs" in getattr(self, "sql", ""):
            return {"Field": "analysis_variant"}
        return None


class RecordingConn:
    def __init__(self, rows=None):
        self.cursor_obj = RecordingCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        return None


def test_stage3a7_select_latest_runs_uses_requested_brand_list():
    conn = RecordingConn()

    stage3a7.select_latest_runs(conn, ["가드렛", "확장브랜드"])

    sql, params = conn.cursor_obj.calls[-1]
    assert "brand IN (%s,%s)" in sql
    assert params == ("가드렛", "확장브랜드", "legacy")


def test_stage3a7_select_latest_runs_filters_requested_variant():
    conn = RecordingConn()

    stage3a7.select_latest_runs(conn, ["가드렛", "확장브랜드"], analysis_variant="short")

    sql, params = conn.cursor_obj.calls[-1]
    assert "analysis_variant = %s" in sql
    assert params == ("가드렛", "확장브랜드", "short")


def test_stage3a7_market_ids_use_requested_brand_list_for_cache_fallback():
    conn = RecordingConn()

    stage3a7.load_market_ids(conn, ["가드렛", "확장브랜드"])

    sql, params = conn.cursor_obj.calls[-1]
    assert "cache_deep_analysis" in sql
    assert "brand IN (%s,%s)" in sql
    assert params == ("가드렛", "확장브랜드")


def test_stage3a7_variant_only_insert_does_not_overwrite_legacy_payload():
    conn = RecordingConn()
    market_ids = {"가드렛": "ML1"}
    short_payloads = {"가드렛": {"analysis_variant": "short", "phenomenon": {"title": "s"}}}
    long_payloads = {"가드렛": {"analysis_variant": "long", "phenomenon": {"title": "l"}}}

    rows = stage3a7.insert_ai_analysis(
        conn,
        payloads={},
        market_ids=market_ids,
        brands=["가드렛"],
        short_payloads=short_payloads,
        long_payloads=long_payloads,
        variants_only=True,
    )

    sql, params = conn.cursor_obj.calls[-1]
    assert "ai_analysis_json" not in sql.split("VALUES", 1)[0]
    assert "ai_analysis_short_json" in sql
    assert "ai_analysis_long_json" in sql
    assert "market_id = VALUES(market_id)" not in sql
    assert params[0:2] == ("가드렛", "ML1")
    assert rows[0]["short_run_id"] is None


def test_event_bundle_processors_are_configured_without_rule_only_tier2():
    assert event_bundle_builder.DIRECT_EVENT_SOURCE_PROCESSORS == (
        "workflow_196_optionB",
        "workflow_196_rev5674",
        "tier2_llm_v1",
        "tier2_llm_v2_rev5671",
    )
    assert event_bundle_builder.CROSS_MATCH_SOURCE_PROCESSORS == ("cross_match_adapter_v1",)
    assert "tier2_exact_rule_v1" not in event_bundle_builder.DIRECT_EVENT_SOURCE_PROCESSORS
    assert "tier2_llm_v2_rev5671" in event_bundle_builder.DIRECT_EVENT_SOURCE_PROCESSORS
