from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass, is_dataclass, replace
from datetime import UTC, datetime
from threading import BoundedSemaphore, Lock
from typing import Generic, TypeVar
from uuid import uuid4

from jw_chat_agent_poc.tools.external.mcp_client import mcp_attempt_limit
from jw_chat_agent_poc.tools.external.telemetry import (
    FailureClass,
    failure_class_from_call,
    failure_class_from_exception,
)

LANE_CONCURRENCY_ENV = "LANE_CONCURRENCY"
_MAX_LANE_CONCURRENCY = 4
_TRANSIENT_FAILURES: frozenset[FailureClass] = frozenset({"connect", "5xx"})
_SECRET_KEY_RE = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.IGNORECASE)
_LOGGER = logging.getLogger("uvicorn.error")
_RUNTIME_LOCK = Lock()
_SEMAPHORES: dict[tuple[str, int], BoundedSemaphore] = {}
_DOWNSHIFTED_PROVIDERS: set[str] = set()

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LaneCallSpec(Generic[T]):
    lane: str
    provider: str
    tool: str
    parameter_summary: Mapping[str, object]
    invoke: Callable[[], T]
    on_error: Callable[[Exception], T]
    retry_transient: bool = True


def lane_concurrency() -> int:
    raw = os.environ.get(LANE_CONCURRENCY_ENV, "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except ValueError:
        return 1
    return value if 1 <= value <= _MAX_LANE_CONCURRENCY else 1


def run_lane_calls(specs: Sequence[LaneCallSpec[T]]) -> list[T]:
    if not specs:
        return []
    results: list[T | None] = [None] * len(specs)
    workers = min(lane_concurrency(), len(specs))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v4-lane") as pool:
        futures = {
            pool.submit(_run_lane_call, spec): index
            for index, spec in enumerate(specs)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    if any(result is None for result in results):
        raise RuntimeError("lane call result was not materialized")
    return [result for result in results if result is not None]


def reset_lane_call_runtime() -> None:
    with _RUNTIME_LOCK:
        _SEMAPHORES.clear()
        _DOWNSHIFTED_PROVIDERS.clear()


def _run_lane_call(spec: LaneCallSpec[T]) -> T:
    call_id = uuid4().hex
    result: T | None = None
    retry_count = 0
    for attempt in (1, 2):
        caught: Exception | None = None
        with ExitStack() as stack:
            stack.enter_context(_lane_guard(spec.lane))
            stack.enter_context(_provider_guard(spec.provider))
            start_wall = datetime.now(UTC)
            start = time.monotonic()
            try:
                stack.enter_context(mcp_attempt_limit(1))
                result = spec.invoke()
            except Exception as exc:  # noqa: BLE001 - converted to lane failure
                caught = exc
            elapsed_ms = round((time.monotonic() - start) * 1000, 3)
            end_wall = datetime.now(UTC)
        failure_class: FailureClass
        if caught is None:
            failure_class = failure_class_from_call(result)
        else:
            failure_class = failure_class_from_exception(caught)
            result = spec.on_error(caught)
        if failure_class == "quota":
            _downshift_provider(spec.provider)
        should_retry = (
            attempt == 1
            and spec.retry_transient
            and failure_class in _TRANSIENT_FAILURES
        )
        if should_retry:
            retry_count += 1
        _emit_lane_call_telemetry(
            spec=spec,
            call_id=call_id,
            attempt=attempt,
            retry_count=retry_count,
            start_ts=start_wall,
            end_ts=end_wall,
            elapsed_ms=elapsed_ms,
            failure_class=failure_class,
            result=result,
        )
        if not should_retry:
            break
    if result is None:
        raise RuntimeError("lane call completed without a result")
    return _annotate_result(result, retry_count=retry_count)


class _SemaphoreGuard:
    def __init__(self, semaphore: BoundedSemaphore) -> None:
        self._semaphore = semaphore

    def __enter__(self) -> None:
        self._semaphore.acquire()

    def __exit__(self, *_args: object) -> None:
        self._semaphore.release()


def _lane_guard(lane: str) -> _SemaphoreGuard:
    return _SemaphoreGuard(_semaphore(f"lane:{lane}", lane_concurrency()))


def _provider_guard(provider: str) -> _SemaphoreGuard:
    with _RUNTIME_LOCK:
        limit = 1 if provider in _DOWNSHIFTED_PROVIDERS else lane_concurrency()
    return _SemaphoreGuard(_semaphore(f"provider:{provider}", limit))


def _semaphore(name: str, limit: int) -> BoundedSemaphore:
    key = (name, limit)
    with _RUNTIME_LOCK:
        semaphore = _SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = BoundedSemaphore(limit)
            _SEMAPHORES[key] = semaphore
        return semaphore


def _downshift_provider(provider: str) -> None:
    with _RUNTIME_LOCK:
        first = provider not in _DOWNSHIFTED_PROVIDERS
        _DOWNSHIFTED_PROVIDERS.add(provider)
    if first:
        _LOGGER.warning("lane_provider_concurrency_downshift provider=%s limit=1", provider)


def _emit_lane_call_telemetry(
    *,
    spec: LaneCallSpec[object],
    call_id: str,
    attempt: int,
    retry_count: int,
    start_ts: datetime,
    end_ts: datetime,
    elapsed_ms: float,
    failure_class: FailureClass,
    result: object,
) -> None:
    payload = {
        "call_id": call_id,
        "lane": spec.lane,
        "provider": spec.provider,
        "tool": spec.tool,
        "parameter_summary": _safe_parameters(spec.parameter_summary),
        "start_ts": start_ts.isoformat(timespec="milliseconds"),
        "end_ts": end_ts.isoformat(timespec="milliseconds"),
        "elapsed_ms": elapsed_ms,
        "status": _result_status(result),
        "attempt": attempt,
        "retry_count": retry_count,
        "failure_class": failure_class,
        "concurrency_limit": lane_concurrency(),
    }
    _LOGGER.info(
        "lane_call_telemetry %s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


def _safe_parameters(parameters: Mapping[str, object]) -> dict[str, str]:
    output: dict[str, str] = {}
    for key, value in sorted(parameters.items())[:8]:
        text = "<redacted>" if _SECRET_KEY_RE.search(str(key)) else str(value)
        output[str(key)[:48]] = " ".join(text.split())[:80]
    return output


def _result_status(result: object) -> str:
    status = str(getattr(result, "status", "")).casefold()
    if status == "no_data":
        return "zero_results"
    if status in {"error", "timeout", "deadline_exceeded"}:
        return "failure"
    return "success"


def _annotate_result(result: T, *, retry_count: int) -> T:
    render_data = getattr(result, "render_data", None)
    if not is_dataclass(result) or not isinstance(render_data, Mapping):
        return result
    return replace(
        result,
        render_data={
            **render_data,
            "lane_call": {
                "retry_count": retry_count,
                "attempts": retry_count + 1,
            },
        },
    )
