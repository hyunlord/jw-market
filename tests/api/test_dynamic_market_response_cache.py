from __future__ import annotations

import threading
import time
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Any

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.response_cache import (
    CacheClaim,
    DynamicMarketOverloadedError,
    DynamicResponseCache,
    DynamicResponseCacheUnavailable,
    MySQLDynamicResponseCacheStore,
    canonical_request_json,
    normalize_json_value,
    select_prune_keys,
)


class MemoryStore:
    def __init__(self) -> None:
        self.epoch = "epoch-1"
        self.rows: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.claim_count = 0

    def source_epoch(self) -> str:
        return self.epoch

    def claim(self, *, cache_key: str, request_json: str, source_epoch: str) -> CacheClaim:
        with self.lock:
            row = self.rows.get(cache_key)
            if row and row["state"] == "ready" and row["epoch"] == source_epoch:
                return CacheClaim.hit(row["payload"])
            if row and row["state"] == "building" and row["epoch"] == source_epoch:
                return CacheClaim.wait()
            self.claim_count += 1
            owner = f"owner-{self.claim_count}"
            attempt_count = int(row.get("attempt_count", 0)) + 1 if row else 1
            self.rows[cache_key] = {
                "state": "building",
                "epoch": source_epoch,
                "owner": owner,
                "attempt_count": attempt_count,
                "failure_reason": None,
                "last_error": None,
            }
            return CacheClaim.build(owner)

    def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None:
        with self.lock:
            assert self.rows[cache_key]["owner"] == lease_owner
            attempt_count = self.rows[cache_key]["attempt_count"]
            self.rows[cache_key] = {
                "state": "ready",
                "epoch": source_epoch,
                "payload": response_json,
                "attempt_count": attempt_count,
                "failure_reason": None,
                "last_error": None,
            }

    def fail(
        self,
        *,
        cache_key: str,
        lease_owner: str,
        failure_reason: str,
        last_error: str | None = None,
    ) -> None:
        with self.lock:
            if self.rows.get(cache_key, {}).get("owner") == lease_owner:
                self.rows[cache_key]["state"] = "failed"
                self.rows[cache_key]["failure_reason"] = failure_reason
                self.rows[cache_key]["last_error"] = last_error


def test_canonical_request_json_normalizes_set_like_lists() -> None:
    first = {"source": "ubist", "filters": {"atc4": ["C10C", "C10A1"], "molecule": ["B", "A"]}}
    second = {"filters": {"molecule": ["A", "B"], "atc4": ["C10A1", "C10C"]}, "source": "ubist"}

    assert canonical_request_json(first) == canonical_request_json(second)


def test_response_cache_hits_without_rebuilding() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    builds = 0

    def build() -> dict[str, object]:
        nonlocal builds
        builds += 1
        return {"status": "SUCCESS", "result": {"value": 1}}

    first = cache.get_or_build({"source": "ubist", "filters": {"atc4": ["C10A1"]}}, build)
    second = cache.get_or_build({"filters": {"atc4": ["C10A1"]}, "source": "ubist"}, build)

    assert first == second
    assert builds == 1
    assert store.claim_count == 1
    stored = json.loads(next(iter(store.rows.values()))["payload"])
    assert stored["__cache_encoding"] == "zlib-base64"


def test_period_windows_do_not_cross_contaminate_response_cache() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    builds = 0

    def request(start: str, end: str) -> dict[str, object]:
        return {
            "view": "general",
            "options": {"period_range": {"start": start, "end": end}},
        }

    def build(value: int) -> dict[str, object]:
        nonlocal builds
        builds += 1
        return {"status": "SUCCESS", "value": value}

    first_a = cache.get_or_build(request("2025-01", "2025-12"), lambda: build(2025))
    window_b = cache.get_or_build(request("2026-01", "2026-04"), lambda: build(2026))
    second_a = cache.get_or_build(request("2025-01", "2025-12"), lambda: build(-1))

    assert first_a == second_a == {"status": "SUCCESS", "value": 2025}
    assert window_b == {"status": "SUCCESS", "value": 2026}
    assert builds == 2
    assert store.claim_count == 2


def test_response_cache_reads_legacy_uncompressed_rows() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    request = {"source": "ubist", "filters": {"atc4": ["C10A1"]}}

    cache.get_or_build(request, lambda: {"version": 1})
    next(iter(store.rows.values()))["payload"] = '{"version":2}'

    assert cache.get_or_build(request, lambda: {"version": 3}) == {"version": 2}


def test_response_cache_releases_lease_when_persistence_fails() -> None:
    class FailingStore(MemoryStore):
        def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None:
            raise DynamicResponseCacheUnavailable("write failed")

    store = FailingStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)

    result = cache.get_or_build({"source": "ubist"}, lambda: {"status": "SUCCESS"})

    assert result == {"status": "SUCCESS"}
    row = next(iter(store.rows.values()))
    assert row["state"] == "failed"
    assert row["failure_reason"] == "persistence_error"
    assert row["last_error"] == "DynamicResponseCacheUnavailable: write failed"


def test_response_cache_can_defer_persistence_until_after_response() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    scheduled: list[object] = []

    result = cache.get_or_build(
        {"source": "ubist"},
        lambda: {"status": "SUCCESS"},
        persistence_scheduler=scheduled.append,
    )

    assert result == {"status": "SUCCESS"}
    assert len(scheduled) == 1
    row = next(iter(store.rows.values()))
    assert row["state"] == "building"

    scheduled[0]()

    assert row is not next(iter(store.rows.values()))
    assert next(iter(store.rows.values()))["state"] == "ready"


def test_response_cache_deferred_persistence_failure_releases_lease(caplog: pytest.LogCaptureFixture) -> None:
    class FailingStore(MemoryStore):
        def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None:
            raise DynamicResponseCacheUnavailable("write failed")

    store = FailingStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    scheduled: list[object] = []

    result = cache.get_or_build(
        {"source": "ubist"},
        lambda: {"status": "SUCCESS"},
        persistence_scheduler=scheduled.append,
    )

    assert result == {"status": "SUCCESS"}
    assert next(iter(store.rows.values()))["state"] == "building"

    scheduled[0]()

    row = next(iter(store.rows.values()))
    assert row["state"] == "failed"
    assert row["failure_reason"] == "persistence_error"
    assert row["last_error"] == "DynamicResponseCacheUnavailable: write failed"
    assert "dynamic_response_cache_store_failed" in caplog.text


def test_response_cache_deferred_persistence_keeps_identical_requests_single_flight() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    scheduled: list[object] = []
    builds = 0

    def build() -> dict[str, object]:
        nonlocal builds
        builds += 1
        return {"status": "SUCCESS"}

    first = cache.get_or_build(
        {"source": "ubist"},
        build,
        persistence_scheduler=scheduled.append,
    )
    results: list[dict[str, object]] = []
    waiter = threading.Thread(target=lambda: results.append(cache.get_or_build({"source": "ubist"}, build)))
    waiter.start()
    time.sleep(0.01)

    assert waiter.is_alive()
    scheduled[0]()
    waiter.join(timeout=1.0)

    assert first == {"status": "SUCCESS"}
    assert results == [first]
    assert builds == 1


def test_response_cache_does_not_hide_unexpected_sync_persistence_bug() -> None:
    class BuggyStore(MemoryStore):
        def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None:
            raise AssertionError("cache invariant broken")

    store = BuggyStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)

    with pytest.raises(AssertionError, match="cache invariant broken"):
        cache.get_or_build({"source": "ubist"}, lambda: {"status": "SUCCESS"})


def test_response_cache_falls_back_to_sync_persistence_when_scheduling_fails() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)

    def reject_schedule(_task: object) -> None:
        raise RuntimeError("scheduler unavailable")

    result = cache.get_or_build(
        {"source": "ubist"},
        lambda: {"status": "SUCCESS"},
        persistence_scheduler=reject_schedule,
    )

    assert result == {"status": "SUCCESS"}
    assert next(iter(store.rows.values()))["state"] == "ready"


def test_response_cache_single_flight_builds_same_key_once() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=2.0)
    barrier = threading.Barrier(5)
    build_lock = threading.Lock()
    build_count = 0
    results: list[dict[str, object]] = []

    def build() -> dict[str, object]:
        nonlocal build_count
        with build_lock:
            build_count += 1
        time.sleep(0.05)
        return {"status": "SUCCESS", "result": {"value": 1}}

    def run() -> None:
        barrier.wait()
        results.append(cache.get_or_build({"source": "ubist", "filters": {"atc4": ["C10A1"]}}, build))

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert build_count == 1
    assert len(results) == 5
    assert all(result == results[0] for result in results)


def test_response_cache_does_not_serve_stale_epoch() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    request = {"source": "ubist", "filters": {"atc4": ["C10A1"]}}

    assert cache.get_or_build(request, lambda: {"version": 1}) == {"version": 1}
    store.epoch = "epoch-2"
    assert cache.get_or_build(request, lambda: {"version": 2}) == {"version": 2}
    assert store.claim_count == 2


def test_response_cache_returns_429_when_distinct_build_slots_are_full() -> None:
    store = MemoryStore()
    semaphore = threading.BoundedSemaphore(1)
    semaphore.acquire()
    cache = DynamicResponseCache(
        store=store,
        build_semaphore=semaphore,
        poll_interval_seconds=0.001,
        wait_timeout_seconds=1.0,
    )

    with pytest.raises(DynamicMarketOverloadedError):
        cache.get_or_build({"source": "ubist", "filters": {"atc4": ["C10A1"]}}, lambda: {"value": 1})

    row = next(iter(store.rows.values()))
    assert row["state"] == "failed"
    assert row["failure_reason"] == "overloaded"


def test_response_cache_normalizes_non_finite_values_before_strict_serialization() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)

    result = cache.get_or_build(
        {"scope": "strategic"},
        lambda: {"data": {"nan": math.nan, "positive_inf": math.inf, "items": [-math.inf]}},
    )

    assert result == {"data": {"nan": None, "positive_inf": None, "items": [None]}}
    stored = next(iter(store.rows.values()))["payload"]
    assert "NaN" not in stored
    assert "Infinity" not in stored


def test_select_prune_keys_removes_expired_then_reduces_capacity_to_low_water() -> None:
    rows = [
        {"cache_key": "expired", "state": "ready", "payload_size": 30, "expired": True, "hit_count": 100, "last_used": 9},
        {"cache_key": "least-used-old", "state": "ready", "payload_size": 40, "expired": False, "hit_count": 0, "last_used": 1},
        {"cache_key": "least-used-new", "state": "ready", "payload_size": 40, "expired": False, "hit_count": 0, "last_used": 2},
        {"cache_key": "popular", "state": "ready", "payload_size": 50, "expired": False, "hit_count": 5, "last_used": 0},
    ]

    assert select_prune_keys(
        rows,
        total_rows=4,
        total_bytes=160,
        max_rows=5,
        max_bytes=160,
        batch_limit=100,
        high_water_ratio=0.9,
        low_water_ratio=0.75,
    ) == ["expired", "least-used-old"]


def test_select_prune_keys_always_cleans_expired_rows_but_never_building_rows() -> None:
    rows = [
        {"cache_key": "active", "state": "building", "payload_size": 100, "expired": True, "hit_count": 0, "last_used": 0},
        {"cache_key": "expired", "state": "ready", "payload_size": 10, "expired": True, "hit_count": 1, "last_used": 1},
        {"cache_key": "fresh", "state": "ready", "payload_size": 10, "expired": False, "hit_count": 0, "last_used": 2},
    ]

    selected = select_prune_keys(
        rows,
        total_rows=3,
        total_bytes=120,
        max_rows=100,
        max_bytes=1_000,
        batch_limit=100,
    )

    assert selected == ["expired"]
    assert "active" not in selected


class _RecordingCursor:
    def __init__(self, *, summary: dict[str, Any] | None = None, candidates: list[dict[str, Any]] | None = None) -> None:
        self.summary = summary or {}
        self.candidates = candidates or []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 0
        self._result_kind = ""

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> int:
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if "COUNT(*) AS total_rows" in normalized:
            self._result_kind = "summary"
            self.rowcount = 1
        elif normalized.startswith("SELECT cache_key"):
            self._result_kind = "candidates"
            self.rowcount = len(self.candidates)
        elif normalized.startswith("DELETE") or normalized.startswith("UPDATE"):
            self._result_kind = "write"
            self.rowcount = 1
        return self.rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self.summary if self._result_kind == "summary" else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.candidates if self._result_kind == "candidates" else []


class _RecordingConnection:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def cursor(self) -> _RecordingCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1


def test_mysql_complete_updates_only_the_claimed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _RecordingCursor()
    connection = _RecordingConnection(cursor)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.response_cache.db.connect", lambda: connection)
    store = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general",
        strategic_dimension_db="strategic",
    )

    store.complete(cache_key="key", lease_owner="owner", source_epoch="epoch", response_json='{"ok":true}')

    sql = "\n".join(statement for statement, _ in cursor.statements)
    assert len(cursor.statements) == 1
    assert cursor.statements[0][0].startswith("UPDATE cache_dynamic_market_response")
    assert "SELECT" not in sql
    assert "DELETE" not in sql
    assert "FOR UPDATE" not in sql
    assert connection.commits == 1


def test_mysql_prune_uses_bounded_candidates_and_observed_value_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 14, 6, 0, 0)
    updated_at = now - timedelta(hours=1)
    candidates = [
        {
            "cache_key": "expired",
            "state": "ready",
            "payload_size": 30,
            "hit_count": 0,
            "last_hit_at": None,
            "expires_at": now - timedelta(minutes=1),
            "updated_at": updated_at,
            "expired": True,
            "last_used": updated_at,
        }
    ]
    cursor = _RecordingCursor(summary={"total_rows": 1, "total_bytes": 30}, candidates=candidates)
    connection = _RecordingConnection(cursor)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.response_cache.db.connect", lambda: connection)
    store = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general",
        strategic_dimension_db="strategic",
    )

    result = store.prune(now=now, grace_seconds=300, batch_limit=100)

    sql = "\n".join(statement for statement, _ in cursor.statements)
    candidate_sql, candidate_params = cursor.statements[1]
    delete_sql, delete_params = cursor.statements[2]
    assert result.selected == 1
    assert result.deleted == 1
    assert result.deleted_bytes == 30
    assert "FOR UPDATE" not in sql
    assert "state <> 'building'" in candidate_sql
    assert "updated_at <= %s" in candidate_sql
    assert candidate_params[-1] == 100
    assert "state = %s" in delete_sql
    assert "updated_at = %s" in delete_sql
    assert "hit_count = %s" in delete_sql
    assert "last_hit_at <=> %s" in delete_sql
    assert delete_params == ("expired", "dynamic", "ready", updated_at, 0, None)
    assert connection.commits == 1


def test_normalize_json_value_reports_nested_non_finite_paths() -> None:
    paths: list[str] = []

    normalized = normalize_json_value({"data": {"series": [1.0, math.nan]}}, on_non_finite=paths.append)

    assert normalized == {"data": {"series": [1.0, None]}}
    assert paths == ["$.data.series[1]"]


def test_response_cache_serves_but_does_not_store_oversized_entry() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(
        store=store,
        max_entry_bytes=8,
        poll_interval_seconds=0.001,
        wait_timeout_seconds=1.0,
    )

    result = cache.get_or_build({"scope": "large"}, lambda: {"payload": "large-enough"})

    assert result == {"payload": "large-enough"}
    row = next(iter(store.rows.values()))
    assert row["state"] == "failed"
    assert row["failure_reason"] == "entry_too_large"


def test_response_cache_records_builder_failure_and_clears_it_after_retry() -> None:
    store = MemoryStore()
    cache = DynamicResponseCache(store=store, poll_interval_seconds=0.001, wait_timeout_seconds=1.0)
    request = {"source": "ubist", "filters": {"atc4": ["C10A1"]}}

    def fail_build() -> dict[str, object]:
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError, match="synthetic failure"):
        cache.get_or_build(request, fail_build)

    failed = next(iter(store.rows.values()))
    assert failed["failure_reason"] == "builder_error"
    assert failed["last_error"] == "ValueError: synthetic failure"
    assert failed["attempt_count"] == 1

    assert cache.get_or_build(request, lambda: {"status": "SUCCESS"}) == {"status": "SUCCESS"}
    ready = next(iter(store.rows.values()))
    assert ready["state"] == "ready"
    assert ready["failure_reason"] is None
    assert ready["last_error"] is None
    assert ready["attempt_count"] == 2


def _explicit_version_fetcher(
    calls: list[str],
    *,
    metric_timestamp: str = "2026-07-14 00:00:00",
    event_timestamp: str = "2026-07-14 01:00:00",
):
    def fake_fetch_all(sql: str, _params: object) -> list[dict[str, object]]:
        calls.append(sql)
        if "computation_version" in sql:
            table_name = next(
                name
                for name in (
                    "mart_general_brand_metric",
                    "mart_general_market_metric",
                    "mart_strategic_ml_brand_metric",
                    "mart_strategic_ml_market_metric",
                    "mart_strategic_cd_brand_metric",
                    "mart_strategic_cd_market_metric",
                )
                if name in sql
            )
            return [{"table_name": table_name, "computation_version": "v3", "computed_at": metric_timestamp}]
        if "d.source, d.dimension_type" in sql:
            table_name = (
                "mart_general_filter_dimension_metric"
                if "mart_general_filter_dimension_metric" in sql
                else "mart_strategic_filter_dimension_metric"
            )
            return [
                {
                    "table_name": table_name,
                    "source": "ubist",
                    "dimension_type": "molecule",
                    "computed_at": "2026-07-13 17:08:13",
                }
            ]
        if "catalog_manifest_hash" in sql:
            return [
                {
                    "table_name": table_name,
                    "source_file_version": "mi-v1",
                    "ingested_at": "2026-07-01 00:00:00",
                    "catalog_manifest_hash": "manifest-1",
                }
                for table_name in ("catalog_ml_market", "catalog_cd_market", "catalog_strategic_brand")
            ]
        if "event_brand_scores" in sql:
            return [
                {
                    "table_name": "event_brand_scores",
                    "workflow_id": 5674,
                    "catalog_version": "mi-v1",
                    "generated_at": event_timestamp,
                }
            ]
        raise AssertionError(f"unexpected source epoch query: {sql}")

    return fake_fetch_all


def test_source_epoch_uses_only_explicit_data_versions(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.response_cache.db.fetch_all",
        _explicit_version_fetcher(calls),
    )
    store = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general_dimension",
        strategic_dimension_db="strategic_dimension",
    )

    assert len(store.source_epoch()) == 64
    combined = "\n".join(calls)
    for table_name in (
        "mart_general_brand_metric",
        "mart_general_market_metric",
        "mart_strategic_ml_brand_metric",
        "mart_strategic_ml_market_metric",
        "mart_strategic_cd_brand_metric",
        "mart_strategic_cd_market_metric",
        "catalog_ml_market",
        "catalog_cd_market",
        "catalog_strategic_brand",
        "mart_general_filter_dimension_metric",
        "mart_strategic_filter_dimension_metric",
    ):
        assert table_name in combined
    for unstable_input in (
        "information_schema.TABLES",
        "CREATE_TIME",
        "UPDATE_TIME",
        "TABLE_ROWS",
        "DATA_LENGTH",
        "INDEX_LENGTH",
    ):
        assert unstable_input not in combined
    assert "computation_version" in combined
    assert combined.count("FORCE INDEX") == 2
    assert "MAX(catalog_manifest_hash)" in combined
    assert "`general_dimension`.`mart_general_filter_dimension_metric`" in combined
    assert "`strategic_dimension`.`mart_strategic_filter_dimension_metric`" in combined


def test_source_epoch_is_stable_until_an_explicit_version_changes(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.response_cache.db.fetch_all",
        _explicit_version_fetcher(calls, metric_timestamp="2026-07-14 00:00:00"),
    )
    baseline = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general_dimension",
        strategic_dimension_db="strategic_dimension",
    ).source_epoch()

    calls.clear()
    same = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general_dimension",
        strategic_dimension_db="strategic_dimension",
    ).source_epoch()
    assert same == baseline

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.response_cache.db.fetch_all",
        _explicit_version_fetcher(calls, metric_timestamp="2026-07-14 00:00:01"),
    )
    changed = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general_dimension",
        strategic_dimension_db="strategic_dimension",
    ).source_epoch()
    assert changed != baseline


def test_deep_section_epoch_includes_event_scores_and_namespace(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.response_cache.db.fetch_all",
        _explicit_version_fetcher(calls),
    )
    store = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general_dimension",
        strategic_dimension_db="strategic_dimension",
        namespace="deep_expensive",
    )

    assert len(store.source_epoch()) == 64
    event_sql = next(sql for sql in calls if "event_brand_scores" in sql)
    assert "generated_at" in event_sql
    assert "workflow_id" in event_sql
    assert "catalog_version" in event_sql
    assert "information_schema" not in event_sql
