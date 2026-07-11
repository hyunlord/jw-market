from __future__ import annotations

import threading
import time

from jw_chat_agent_poc.tools.query_layer.layer import StrategicQueryLayer
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

    assert reader.calls == 2
    assert first is not second
