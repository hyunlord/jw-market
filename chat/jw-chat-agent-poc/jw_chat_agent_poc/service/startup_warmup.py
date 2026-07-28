from __future__ import annotations

from collections.abc import Callable
import logging
import os
import threading
import time
from typing import Final, Protocol, final

from jw_chat_agent_poc.tools.query_layer.store import MART_TTL_ENV, MartSnapshot, shared_strategic_mart_store


LOGGER = logging.getLogger("uvicorn.error")
WARMUP_ENABLED_ENV: Final = "CHAT_STARTUP_WARMUP_ENABLED"
# The store owns the TTL now, so this only stays as the historical alias for the env
# name; duplicating the literal here is how the two call sites drifted apart before.
WARMUP_TTL_ENV: Final = MART_TTL_ENV


class StartupWarmup(Protocol):
    def start(self) -> None: ...

    def is_ready(self) -> bool: ...


@final
class DisabledStartupWarmup:
    def start(self) -> None:
        return

    def is_ready(self) -> bool:
        return True


@final
class StrategicMartStartupWarmup:
    """Load the shared mart snapshot once and expose completion to readiness."""

    def __init__(self, load_snapshot: Callable[[], MartSnapshot]) -> None:
        self._load_snapshot = load_snapshot
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            thread = threading.Thread(
                target=self._run,
                name="strategic-mart-startup-warmup",
                daemon=True,
            )
            thread.start()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def wait_until_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout=timeout_s)

    def _run(self) -> None:
        started_at = time.monotonic()
        LOGGER.info("strategic mart startup warmup started")
        try:
            snapshot = self._load_snapshot()
        except Exception:  # noqa: BLE001 - process readiness boundary logs and stays closed
            LOGGER.exception(
                "strategic mart startup warmup failed elapsed_s=%.3f",
                time.monotonic() - started_at,
            )
            return
        self._ready.set()
        LOGGER.info(
            "strategic mart startup warmup completed elapsed_s=%.3f records=%d",
            time.monotonic() - started_at,
            len(snapshot.records),
        )


def startup_warmup_from_env() -> StartupWarmup:
    enabled = os.environ.get(WARMUP_ENABLED_ENV, "true").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return DisabledStartupWarmup()
    return StrategicMartStartupWarmup(
        lambda: shared_strategic_mart_store().snapshot()
    )
