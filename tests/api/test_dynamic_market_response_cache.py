from __future__ import annotations

import threading
import time
import json
import math
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
    select_eviction_keys,
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
            self.rows[cache_key] = {"state": "building", "epoch": source_epoch, "owner": owner}
            return CacheClaim.build(owner)

    def complete(self, *, cache_key: str, lease_owner: str, source_epoch: str, response_json: str) -> None:
        with self.lock:
            assert self.rows[cache_key]["owner"] == lease_owner
            self.rows[cache_key] = {"state": "ready", "epoch": source_epoch, "payload": response_json}

    def fail(self, *, cache_key: str, lease_owner: str) -> None:
        with self.lock:
            if self.rows.get(cache_key, {}).get("owner") == lease_owner:
                self.rows[cache_key]["state"] = "failed"


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
    assert next(iter(store.rows.values()))["state"] == "failed"


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

    assert next(iter(store.rows.values()))["state"] == "failed"


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


def test_select_eviction_keys_expires_first_then_uses_lfu_lru() -> None:
    rows = [
        {"cache_key": "expired", "payload_size": 30, "expired": True, "hit_count": 100, "last_used": 9},
        {"cache_key": "least-used-old", "payload_size": 40, "expired": False, "hit_count": 0, "last_used": 1},
        {"cache_key": "least-used-new", "payload_size": 40, "expired": False, "hit_count": 0, "last_used": 2},
        {"cache_key": "popular", "payload_size": 40, "expired": False, "hit_count": 5, "last_used": 0},
    ]

    assert select_eviction_keys(
        rows,
        incoming_size=40,
        max_rows=5,
        max_bytes=160,
        high_water_ratio=0.9,
        low_water_ratio=0.75,
    ) == [
        "expired",
        "least-used-old",
    ]


def test_select_eviction_keys_orders_expired_rows_by_age_before_hit_count() -> None:
    rows = [
        {"cache_key": "expired-old-hit", "payload_size": 50, "expired": True, "hit_count": 9, "last_used": 1},
        {"cache_key": "expired-new-never-hit", "payload_size": 50, "expired": True, "hit_count": 0, "last_used": 2},
        {"cache_key": "ready-never-hit", "payload_size": 50, "expired": False, "hit_count": 0, "last_used": 0},
    ]

    assert select_eviction_keys(
        rows,
        incoming_size=50,
        max_rows=4,
        max_bytes=200,
        high_water_ratio=0.9,
        low_water_ratio=0.75,
    ) == ["expired-old-hit"]


def test_select_eviction_keys_counts_but_never_selects_active_builds() -> None:
    rows = [
        {"cache_key": "active", "state": "building", "payload_size": 0, "hit_count": 0, "last_used": 0},
        {"cache_key": "ready-old", "state": "ready", "payload_size": 50, "hit_count": 0, "last_used": 1},
        {"cache_key": "ready-new", "state": "ready", "payload_size": 50, "hit_count": 0, "last_used": 2},
    ]

    selected = select_eviction_keys(
        rows,
        incoming_size=50,
        max_rows=4,
        max_bytes=200,
        high_water_ratio=0.9,
        low_water_ratio=0.75,
    )

    assert selected == ["ready-old"]
    assert "active" not in selected

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
    assert next(iter(store.rows.values()))["state"] == "failed"


def test_source_epoch_covers_general_strategic_dimension_and_catalog_reads(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, _params: object) -> list[dict[str, object]]:
        calls.append(sql)
        if "information_schema.TABLES" in sql:
            return [
                {
                    "TABLE_SCHEMA": "mart",
                    "TABLE_NAME": "catalog_ml_market",
                    "CREATE_TIME": "t1",
                    "UPDATE_TIME": "t2",
                    "TABLE_ROWS": 15,
                }
            ]
        if "catalog_manifest_hash" in sql:
            return [{"table_name": "catalog_ml_market", "catalog_manifest_hash": "manifest-1"}]
        table_name = next(name for name in (
            "mart_general_market_metric",
            "mart_strategic_ml_market_metric",
            "mart_strategic_cd_market_metric",
        ) if name in sql)
        return [{"table_name": table_name, "source": "ubist", "measure": "sales", "computed_at": "t1", "period_count": 12}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.response_cache.db.fetch_all", fake_fetch_all)
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
    assert "UPDATE_TIME" in combined
    assert "TABLE_ROWS" in combined
    assert "MAX(catalog_manifest_hash)" in combined
    assert combined.count("MAX(computed_at)") == 3
    assert "`general_dimension`.`mart_general_filter_dimension_metric`" not in combined
    assert "`strategic_dimension`.`mart_strategic_filter_dimension_metric`" not in combined


def test_deep_section_epoch_includes_event_scores_and_namespace(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_fetch_all(sql: str, params: object) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "information_schema.TABLES" in sql:
            return [
                {
                    "TABLE_SCHEMA": "mart",
                    "TABLE_NAME": "event_brand_scores" if params == ("mart",) else "catalog_ml_market",
                    "CREATE_TIME": "t1",
                    "UPDATE_TIME": "t2",
                    "TABLE_ROWS": 1,
                }
            ]
        if "catalog_manifest_hash" in sql:
            return [{"table_name": "catalog_ml_market", "catalog_manifest_hash": "manifest-1"}]
        return [{"table_name": "mart", "source": "ubist", "measure": "sales", "computed_at": "t1", "period_count": 12}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.response_cache.db.fetch_all", fake_fetch_all)
    store = MySQLDynamicResponseCacheStore(
        mart_db="mart",
        general_dimension_db="general_dimension",
        strategic_dimension_db="strategic_dimension",
        namespace="deep_expensive",
    )

    assert len(store.source_epoch()) == 64
    assert any(params == ("mart",) and "event_brand_scores" in sql for sql, params in calls)
