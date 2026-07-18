from __future__ import annotations

from collections.abc import Callable
import threading
import time

from jw_chat_agent_poc.tools.query_layer.layer import QueryResultStore, StrategicQueryLayer
from jw_chat_agent_poc.tools.query_layer.store import MartSnapshot, TtlStrategicMartStore


class CountingReader:
    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.calls = 0
        self.delay_s = delay_s
        self._lock = threading.Lock()

    def load(self) -> MartSnapshot:
        with self._lock:
            self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        return MartSnapshot((), time.monotonic())


class BlockingRefreshReader:
    def __init__(self) -> None:
        self.calls = 0
        self.refresh_started = threading.Event()
        self.release_refresh = threading.Event()

    def load(self) -> MartSnapshot:
        self.calls += 1
        if self.calls > 1:
            self.refresh_started.set()
            self.release_refresh.wait(timeout=2)
        return MartSnapshot((), time.monotonic())


class FailingRefreshReader:
    def __init__(self) -> None:
        self.calls = 0

    def load(self) -> MartSnapshot:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("refresh failed")
        return MartSnapshot((), time.monotonic())


def wait_until(predicate: Callable[[], bool], *, timeout_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def test_default_layers_share_store_but_isolate_query_results(monkeypatch) -> None:
    shared_store = TtlStrategicMartStore(CountingReader(), prewarm=False)
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.query_layer.layer.shared_strategic_mart_store",
        lambda ttl_seconds: shared_store,
    )

    first = StrategicQueryLayer()
    second = StrategicQueryLayer()

    assert first._store is second._store is shared_store
    assert first._results is not second._results
    assert first._results.put([{"brand": "A"}]) == "qr_0001"
    assert second._results.put([{"brand": "B"}]) == "qr_0001"
    assert first._results.get("qr_0001") == [{"brand": "A"}]
    assert second._results.get("qr_0001") == [{"brand": "B"}]


def test_query_result_store_allocates_unique_ids_under_concurrent_writes() -> None:
    class YieldingCounter:
        def __add__(self, _value: int) -> int:
            time.sleep(0.02)
            return 1

    store = QueryResultStore()
    object.__setattr__(store, "_counter", YieldingCounter())
    start = threading.Barrier(3)
    result_ids: list[str] = []

    def put(brand: str) -> None:
        start.wait(timeout=1)
        result_ids.append(store.put([{"brand": brand}]))

    threads = [threading.Thread(target=put, args=(brand,)) for brand in ("A", "B")]
    for thread in threads:
        thread.start()
    start.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=1)

    assert sorted(result_ids) == ["qr_0001", "qr_0002"]
    assert store.get("qr_0001") != store.get("qr_0002")


def test_injected_readers_keep_private_stores() -> None:
    first = StrategicQueryLayer(reader=CountingReader())
    second = StrategicQueryLayer(reader=CountingReader())

    assert first._store is not second._store


def test_injected_store_is_used_without_replacement() -> None:
    store = TtlStrategicMartStore(CountingReader(), prewarm=False)

    layer = StrategicQueryLayer(store=store)

    assert layer._store is store


def test_strategic_store_single_flight_shares_one_cold_load() -> None:
    reader = CountingReader(delay_s=0.05)
    store = TtlStrategicMartStore(reader, prewarm=False)
    snapshots: list[MartSnapshot] = []

    threads = [threading.Thread(target=lambda: snapshots.append(store.snapshot())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert reader.calls == 1
    assert len(snapshots) == 4
    assert all(snapshot is snapshots[0] for snapshot in snapshots)


def test_strategic_store_reloads_after_ttl_expiry() -> None:
    reader = CountingReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)

    first = store.snapshot()
    second = store.snapshot()

    assert second is first
    assert wait_until(lambda: store._snapshot is not first)
    assert reader.calls == 2


def test_expired_snapshot_returns_stale_value_while_refresh_runs() -> None:
    reader = BlockingRefreshReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)
    first = store.snapshot()
    returned: list[MartSnapshot] = []
    finished = threading.Event()

    def read_expired_snapshot() -> None:
        returned.append(store.snapshot())
        finished.set()

    thread = threading.Thread(target=read_expired_snapshot)
    thread.start()
    try:
        assert reader.refresh_started.wait(timeout=1)
        assert finished.wait(timeout=0.05)
        assert returned == [first]
    finally:
        reader.release_refresh.set()
        thread.join(timeout=2)


def test_concurrent_expired_reads_start_one_refresh() -> None:
    reader = BlockingRefreshReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)
    first = store.snapshot()
    snapshots: list[MartSnapshot] = []

    threads = [threading.Thread(target=lambda: snapshots.append(store.snapshot())) for _ in range(6)]
    for thread in threads:
        thread.start()
    try:
        assert reader.refresh_started.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=0.1)
        assert snapshots == [first] * 6
        assert reader.calls == 2
    finally:
        reader.release_refresh.set()
        for thread in threads:
            thread.join(timeout=2)


def test_failed_refresh_preserves_snapshot_and_allows_retry() -> None:
    reader = FailingRefreshReader()
    store = TtlStrategicMartStore(reader, ttl_seconds=0, prewarm=False)
    first = store.snapshot()

    assert store.snapshot() is first
    assert wait_until(lambda: reader.calls == 2 and not store._refreshing)
    assert store._snapshot is first

    assert store.snapshot() is first
    assert wait_until(lambda: reader.calls == 3 and store._snapshot is not first)
