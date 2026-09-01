from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import time

from jw_chat_agent_poc.tool_use.contracts import ToolEnvelope
from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ExecutableTool,
    ToolExecutionRecord,
    ToolFailureRecord,
)


@dataclass(frozen=True, slots=True)
class PreparedCall:
    index: int
    tool: ExecutableTool
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class _CompletedCall:
    raw_result: object | None
    latency_ms: float
    error: Exception | None


def execute_parallel(
    calls: Sequence[PreparedCall],
    *,
    max_workers: int,
) -> tuple[list[ToolExecutionRecord], list[ToolFailureRecord]]:
    if not calls:
        return [], []

    pool = ThreadPoolExecutor(
        max_workers=min(max_workers, len(calls)),
        thread_name_prefix="v3-tool-shadow",
    )
    submitted_at = time.monotonic()
    pending: dict[Future[_CompletedCall], PreparedCall] = {
        pool.submit(_run_tool, call): call for call in calls
    }
    deadlines = {
        future: submitted_at + call.tool.timeout_s
        for future, call in pending.items()
    }
    completed: list[tuple[int, ToolExecutionRecord]] = []
    failures: list[tuple[int, ToolFailureRecord]] = []
    try:
        while pending:
            _expire_calls(pending, deadlines, failures)
            if not pending:
                break
            timeout = max(
                0.0,
                min(deadlines[future] for future in pending) - time.monotonic(),
            )
            done, _ = wait(
                tuple(pending),
                timeout=timeout,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                call = pending.pop(future)
                _record_outcome(call, future.result(), completed, failures)
    finally:
        # A timed-out read may still be unwinding inside its bounded SDK timeout.
        # Do not let that delay this SHADOW bundle or the selection observer.
        pool.shutdown(wait=False, cancel_futures=True)
    return (
        [record for _, record in sorted(completed, key=lambda item: item[0])],
        [record for _, record in sorted(failures, key=lambda item: item[0])],
    )


def _expire_calls(
    pending: dict[Future[_CompletedCall], PreparedCall],
    deadlines: dict[Future[_CompletedCall], float],
    failures: list[tuple[int, ToolFailureRecord]],
) -> None:
    now = time.monotonic()
    expired = [
        future
        for future in pending
        if not future.done() and deadlines[future] <= now
    ]
    for future in expired:
        call = pending.pop(future)
        future.cancel()
        failures.append(
            (
                call.index,
                ToolFailureRecord(
                    call.tool.name,
                    call.arguments,
                    "execution",
                    "TOOL_TIMEOUT",
                    f"tool exceeded {call.tool.timeout_s:.3f}s timeout",
                    call.tool.timeout_s * 1000,
                ),
            )
        )


def _record_outcome(
    call: PreparedCall,
    outcome: _CompletedCall,
    completed: list[tuple[int, ToolExecutionRecord]],
    failures: list[tuple[int, ToolFailureRecord]],
) -> None:
    if outcome.error is not None:
        failures.append(
            (
                call.index,
                ToolFailureRecord(
                    call.tool.name,
                    call.arguments,
                    "execution",
                    type(outcome.error).__name__,
                    str(outcome.error),
                    outcome.latency_ms,
                ),
            )
        )
        return

    raw_result = outcome.raw_result
    if isinstance(raw_result, ToolEnvelope) and not raw_result.ok:
        failures.append(
            (
                call.index,
                ToolFailureRecord(
                    call.tool.name,
                    call.arguments,
                    "execution",
                    raw_result.error_code or "NO_EVIDENCE",
                    raw_result.error_message or raw_result.preview,
                    outcome.latency_ms,
                ),
            )
        )
        return
    completed.append(
        (
            call.index,
            ToolExecutionRecord(
                call.tool.name,
                call.arguments,
                raw_result,
                outcome.latency_ms,
            ),
        )
    )


def _run_tool(call: PreparedCall) -> _CompletedCall:
    started = time.monotonic()
    try:
        return _CompletedCall(
            raw_result=call.tool.execute(call.arguments),
            latency_ms=(time.monotonic() - started) * 1000,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 - every tool failure is evidence
        return _CompletedCall(
            raw_result=None,
            latency_ms=(time.monotonic() - started) * 1000,
            error=exc,
        )
