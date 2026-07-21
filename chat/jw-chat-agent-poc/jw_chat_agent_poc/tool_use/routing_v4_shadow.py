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


@dataclass(frozen=True, slots=True)
class ShadowTask(Generic[T]):
    result_queue: Queue[ShadowOutcome[T]]


def start_with_budget(operation: Callable[[], T]) -> ShadowTask[T]:
    """Start a SHADOW operation without waiting on the response path."""

    result_queue: Queue[ShadowOutcome[T]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            outcome = ShadowOutcome(status="ok", value=operation())
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK - SHADOW must never fail legacy.
            outcome = ShadowOutcome[T](status="error", error_name=type(exc).__name__)
        result_queue.put_nowait(outcome)

    Thread(target=worker, name="routing-v4-shadow", daemon=True).start()
    return ShadowTask(result_queue=result_queue)


def collect_with_budget(task: ShadowTask[T]) -> ShadowOutcome[T]:
    """Wait only the configured response-path budget for a started task."""

    try:
        return task.result_queue.get(timeout=_configured_budget_ms() / 1000)
    except Empty:
        return ShadowOutcome(status="budget_exceeded")


def run_with_budget(operation: Callable[[], T]) -> ShadowOutcome[T]:
    """Start and collect a SHADOW operation within one bounded call."""

    return collect_with_budget(start_with_budget(operation))


def _configured_budget_ms() -> int:
    raw = os.environ.get(SHADOW_MAX_WAIT_MS_FLAG, str(_DEFAULT_MAX_WAIT_MS))
    try:
        configured = int(raw)
    except ValueError:
        configured = _DEFAULT_MAX_WAIT_MS
    return min(max(configured, 0), _MAX_WAIT_MS)
