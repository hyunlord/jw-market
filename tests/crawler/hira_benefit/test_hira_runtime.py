from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

from pipeline.scripts.crawler.hira_benefit.runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    terminate_process_group,
)


def test_no_output_heartbeat_interval_is_30_seconds() -> None:
    assert HEARTBEAT_INTERVAL_SECONDS == 30


def test_cleanup_targets_process_group_before_direct_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, signal.Signals]] = []
    waited: list[float] = []
    process = SimpleNamespace(pid=1234, returncode=None)

    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    async def fake_wait_for(awaitable: object, timeout: float) -> int:
        waited.append(timeout)
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        process.returncode = 0
        return 0

    async def fake_wait() -> int:
        return 0

    process.wait = fake_wait
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    asyncio.run(terminate_process_group(process, grace_seconds=10))

    assert calls == [(1234, signal.SIGTERM)]
    assert waited == [10]
