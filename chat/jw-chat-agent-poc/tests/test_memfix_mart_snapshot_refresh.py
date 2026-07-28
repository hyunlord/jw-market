from __future__ import annotations

from collections.abc import Callable
import threading
import time

from jw_chat_agent_poc.tools.query_layer import store as store_module
from jw_chat_agent_poc.tools.query_layer.store import (
    DEFAULT_MART_TTL_SECONDS,
    MART_TTL_ENV,
    MartSnapshot,
    TtlStrategicMartStore,
    mart_ttl_seconds,
    shared_strategic_mart_store,
)


def wait_until(predicate: Callable[[], bool], *, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


class FingerprintReader:
    """Reader that reports a change signal the store can probe."""

    def __init__(self, *, fingerprint: str = "same") -> None:
        self.load_calls = 0
        self.fingerprint_calls = 0
        self._fingerprint = fingerprint
        self._lock = threading.Lock()

    def set_fingerprint(self, value: str) -> None:
        with self._lock:
            self._fingerprint = value

    def fingerprint(self) -> str | None:
        with self._lock:
            self.fingerprint_calls += 1
            return self._fingerprint

    def load(self) -> MartSnapshot:
        with self._lock:
            self.load_calls += 1
        return MartSnapshot((), time.monotonic())


class NoFingerprintReader:
    """Pre-existing shape: only load(). Must keep the original rebuild-every-time path."""

    def __init__(self) -> None:
        self.load_calls = 0

    def load(self) -> MartSnapshot:
        self.load_calls += 1
        return MartSnapshot((), time.monotonic())


class ProbeRaisingReader:
    def __init__(self) -> None:
        self.load_calls = 0

    def fingerprint(self) -> str | None:
        raise RuntimeError("probe exploded")

    def load(self) -> MartSnapshot:
        self.load_calls += 1
        return MartSnapshot((), time.monotonic())


class ProbeNoneReader:
    def __init__(self) -> None:
        self.load_calls = 0

    def fingerprint(self) -> str | None:
        return None

    def load(self) -> MartSnapshot:
        self.load_calls += 1
        return MartSnapshot((), time.monotonic())


# --------------------------------------------------------------------------------------
# TTL resolution -- one value for the whole process
# --------------------------------------------------------------------------------------


def test_ttl_defaults_to_300_when_env_absent(monkeypatch) -> None:
    monkeypatch.delenv(MART_TTL_ENV, raising=False)
    assert mart_ttl_seconds() == DEFAULT_MART_TTL_SECONDS == 300


def test_ttl_reads_env(monkeypatch) -> None:
    monkeypatch.setenv(MART_TTL_ENV, "21600")
    assert mart_ttl_seconds() == 21600


def test_ttl_rejects_junk_and_non_positive(monkeypatch) -> None:
    for bad in ("", "abc", "0", "-5", "12.5"):
        monkeypatch.setenv(MART_TTL_ENV, bad)
        assert mart_ttl_seconds() == DEFAULT_MART_TTL_SECONDS, bad


def test_shared_store_is_one_instance_regardless_of_ttl(monkeypatch) -> None:
    """A second store means a second multi-GiB snapshot resident. There must be one."""
    monkeypatch.setattr(store_module, "_SHARED_MART_STORE", None)
    monkeypatch.setattr(
        store_module, "TtlStrategicMartStore", lambda *a, **k: object(), raising=True
    )
    monkeypatch.setenv(MART_TTL_ENV, "300")
    first = shared_strategic_mart_store()
    monkeypatch.setenv(MART_TTL_ENV, "21600")
    second = shared_strategic_mart_store()
    assert first is second, "changing the TTL env must not mint a second shared store"


def test_shared_store_takes_ttl_from_env(monkeypatch) -> None:
    monkeypatch.setattr(store_module, "_SHARED_MART_STORE", None)
    captured: dict[str, object] = {}

    def fake_store(reader, ttl_seconds):  # noqa: ANN001 - test double
        captured["ttl"] = ttl_seconds
        return object()

    monkeypatch.setattr(store_module, "TtlStrategicMartStore", fake_store, raising=True)
    monkeypatch.setattr(store_module, "MariaDbStrategicMartReader", lambda: object(), raising=True)
    monkeypatch.setenv(MART_TTL_ENV, "21600")
    shared_strategic_mart_store()
    assert captured["ttl"] == 21600


# --------------------------------------------------------------------------------------
# Change-detection guard -- the doubling only has to happen when rows actually changed
# --------------------------------------------------------------------------------------


def test_unchanged_fingerprint_skips_the_rebuild() -> None:
    reader = FingerprintReader(fingerprint="9975|2026-06-30 03:26:10")
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    first = store.snapshot()
    assert reader.load_calls == 1

    # TTL is 0, so the next access is due for a refresh.
    served = store.snapshot()
    assert wait_until(lambda: reader.fingerprint_calls >= 2)
    assert wait_until(lambda: store._refreshing is False)

    assert reader.load_calls == 1, "unchanged rows must not trigger a second full load"
    assert served is first, "the already-built snapshot keeps being served"
    assert store.snapshot() is first


def test_changed_fingerprint_still_rebuilds() -> None:
    reader = FingerprintReader(fingerprint="9975|2026-06-30 03:26:10")
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    first = store.snapshot()
    assert reader.load_calls == 1

    reader.set_fingerprint("9976|2026-07-28 00:00:00")
    store.snapshot()
    assert wait_until(lambda: reader.load_calls == 2)
    assert wait_until(lambda: store.snapshot() is not first)


def test_skipping_extends_freshness_so_it_does_not_probe_every_call() -> None:
    reader = FingerprintReader(fingerprint="stable")
    store = TtlStrategicMartStore(reader, ttl_seconds=3600, prewarm=False)

    store.snapshot()
    assert reader.load_calls == 1
    probes_after_load = reader.fingerprint_calls

    for _ in range(5):
        store.snapshot()
    assert reader.fingerprint_calls == probes_after_load, "TTL not elapsed -> no probing"
    assert reader.load_calls == 1


def test_reader_without_fingerprint_keeps_rebuilding() -> None:
    """Existing readers only implement load(); behaviour must be unchanged for them."""
    reader = NoFingerprintReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    store.snapshot()
    assert reader.load_calls == 1
    store.snapshot()
    assert wait_until(lambda: reader.load_calls == 2)


def test_probe_exception_falls_back_to_rebuild() -> None:
    reader = ProbeRaisingReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    store.snapshot()
    assert reader.load_calls == 1
    store.snapshot()
    assert wait_until(lambda: reader.load_calls == 2), "a broken probe must not skip refresh"


def test_probe_returning_none_falls_back_to_rebuild() -> None:
    reader = ProbeNoneReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    store.snapshot()
    assert reader.load_calls == 1
    store.snapshot()
    assert wait_until(lambda: reader.load_calls == 2), "unknown state must rebuild"


def test_refresh_failure_still_preserves_the_served_snapshot() -> None:
    class FailingLoadReader:
        def __init__(self) -> None:
            self.load_calls = 0
            self.fp = "a"

        def fingerprint(self) -> str | None:
            return self.fp

        def load(self) -> MartSnapshot:
            self.load_calls += 1
            if self.load_calls == 2:
                raise RuntimeError("refresh failed")
            return MartSnapshot((), time.monotonic())

    reader = FailingLoadReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)
    first = store.snapshot()
    reader.fp = "b"
    store.snapshot()
    assert wait_until(lambda: reader.load_calls == 2)
    assert wait_until(lambda: store._refreshing is False)
    assert store.snapshot() is first, "a failed refresh must keep serving the old snapshot"


def test_snapshot_object_is_never_mutated_by_a_skip() -> None:
    """Freshness is tracked in the store, so loaded_at on the snapshot stays put.

    Replacing the frozen snapshot to move a timestamp would rebuild
    DerivedSnapshotIndex, which is exactly the cost the skip is meant to avoid.
    """
    reader = FingerprintReader(fingerprint="stable")
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)
    first = store.snapshot()
    original_loaded_at = first.loaded_at
    original_derived = first.derived

    store.snapshot()
    assert wait_until(lambda: reader.fingerprint_calls >= 2)
    assert wait_until(lambda: store._refreshing is False)

    assert store.snapshot() is first
    assert first.loaded_at == original_loaded_at
    assert first.derived is original_derived

# --------------------------------------------------------------------------------------
# Observability -- a skip must stay visible, and must not be booked as a rebuild
# --------------------------------------------------------------------------------------


def test_skip_is_counted_separately_from_a_rebuild() -> None:
    """refresh_successes keeps meaning "a rebuild completed".

    Without a separate counter the skip path would make refresh_successes flatten to a
    constant, and an operator watching it would conclude refreshing had stopped.
    """
    reader = FingerprintReader(fingerprint="stable")
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    store.snapshot()
    after_load = store.observability()
    assert after_load["refresh_successes"] == 1
    assert after_load["refresh_skips"] == 0
    assert after_load["refresh_failures"] == 0

    store.snapshot()
    assert wait_until(lambda: store.observability()["refresh_skips"] == 1)

    metrics = store.observability()
    assert metrics["refresh_successes"] == 1, "a skip is not a rebuild"
    assert metrics["refresh_skips"] == 1
    assert metrics["refresh_failures"] == 0
    assert reader.load_calls == 1


def test_snapshot_age_still_reports_time_since_the_build() -> None:
    """A skip extends TTL freshness but must not backdate the snapshot's age.

    snapshot_age_seconds answers "how old is the data I am serving", which is time
    since it was built -- not since freshness was last confirmed.
    """
    reader = FingerprintReader(fingerprint="stable")
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)
    first = store.snapshot()

    store.snapshot()
    assert wait_until(lambda: store.observability()["refresh_skips"] == 1)

    metrics = store.observability()
    assert metrics["snapshot_age_seconds"] is not None
    assert metrics["row_count"] == len(first.records)
