from __future__ import annotations

import stage3a7_create_and_insert_ai_analysis as stage3a7
from bundle_builder import event_bundle_builder


class RecordingCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, tuple(params or ())))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None


class RecordingConn:
    def __init__(self, rows=None):
        self.cursor_obj = RecordingCursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_stage3a7_select_latest_runs_uses_requested_brand_list():
    conn = RecordingConn()

    stage3a7.select_latest_runs(conn, ["가드렛", "확장브랜드"])

    sql, params = conn.cursor_obj.calls[-1]
    assert "brand IN (%s,%s)" in sql
    assert params == ("가드렛", "확장브랜드")


def test_stage3a7_market_ids_use_requested_brand_list_for_cache_fallback():
    conn = RecordingConn()

    stage3a7.load_market_ids(conn, ["가드렛", "확장브랜드"])

    sql, params = conn.cursor_obj.calls[-1]
    assert "cache_deep_analysis" in sql
    assert "brand IN (%s,%s)" in sql
    assert params == ("가드렛", "확장브랜드")


def test_event_bundle_processors_are_configured_without_rule_only_tier2():
    assert event_bundle_builder.DIRECT_EVENT_SOURCE_PROCESSORS == ("workflow_196_optionB",)
    assert event_bundle_builder.CROSS_MATCH_SOURCE_PROCESSORS == ("cross_match_adapter_v1",)
    assert "tier2_exact_rule_v1" not in event_bundle_builder.DIRECT_EVENT_SOURCE_PROCESSORS
