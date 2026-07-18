from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from jw_chat_agent_poc.service.app import create_app
from jw_chat_agent_poc.service.startup_warmup import StrategicMartStartupWarmup
from jw_chat_agent_poc.tools.query_layer.store import MartSnapshot


class ControllableWarmup:
    def __init__(self) -> None:
        self.started = False
        self.ready = False

    def start(self) -> None:
        self.started = True

    def is_ready(self) -> bool:
        return self.ready


def test_readiness_waits_for_startup_warmup_while_liveness_stays_healthy() -> None:
    warmup = ControllableWarmup()
    app = create_app(startup_warmup=warmup)

    with TestClient(app) as client:
        assert warmup.started is True
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503

        warmup.ready = True

        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_warmup_becomes_ready_only_after_snapshot_load_completes() -> None:
    load_started = threading.Event()
    release_load = threading.Event()
    load_completed = threading.Event()

    def load_snapshot() -> MartSnapshot:
        load_started.set()
        _ = release_load.wait(timeout=1)
        load_completed.set()
        return MartSnapshot((), 0.0)

    warmup = StrategicMartStartupWarmup(load_snapshot)

    warmup.start()
    assert load_started.wait(timeout=1) is True
    assert warmup.is_ready() is False

    release_load.set()
    assert load_completed.wait(timeout=1) is True
    assert warmup.wait_until_ready(timeout_s=1) is True


def test_warmup_start_is_idempotent() -> None:
    loaded = threading.Event()
    calls = 0

    def load_snapshot() -> MartSnapshot:
        nonlocal calls
        calls += 1
        loaded.set()
        return MartSnapshot((), 0.0)

    warmup = StrategicMartStartupWarmup(load_snapshot)

    warmup.start()
    warmup.start()

    assert loaded.wait(timeout=1) is True
    assert calls == 1
