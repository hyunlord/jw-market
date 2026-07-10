from __future__ import annotations

import threading
import time
import json
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
    canonical_request_json,
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
