from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from queue import Empty, Queue
from threading import Thread
from typing import Generic, TypeVar


T = TypeVar("T")
SHADOW_MAX_WAIT_MS_FLAG = "CHAT_TOOL_ROUTING_SHADOW_MAX_WAIT_MS"
_DEFAULT_MAX_WAIT_MS = 25
_MAX_WAIT_MS = 100


@dataclass(frozen=True, slots=True)
class ShadowOutcome(Generic[T]):
    status: str
    value: T | None = None
    error_name: str | None = None


def run_with_budget(operation: Callable[[], T]) -> ShadowOutcome[T]:
    """Bound response-path waiting while allowing SHADOW diagnostics to finish."""

    result_queue: Queue[ShadowOutcome[T]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            outcome = ShadowOutcome(status="ok", value=operation())
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK - SHADOW must never fail legacy.
            outcome = ShadowOutcome[T](status="error", error_name=type(exc).__name__)
        result_queue.put_nowait(outcome)

    Thread(target=worker, name="routing-v4-shadow", daemon=True).start()
    try:
        return result_queue.get(timeout=_configured_budget_ms() / 1000)
    except Empty:
        return ShadowOutcome(status="budget_exceeded")


def _configured_budget_ms() -> int:
    raw = os.environ.get(SHADOW_MAX_WAIT_MS_FLAG, str(_DEFAULT_MAX_WAIT_MS))
    try:
        configured = int(raw)
    except ValueError:
        configured = _DEFAULT_MAX_WAIT_MS
    return min(max(configured, 0), _MAX_WAIT_MS)
