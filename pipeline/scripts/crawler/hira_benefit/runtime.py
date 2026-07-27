from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HEARTBEAT_INTERVAL_SECONDS = 30
PROCESS_GROUP_GRACE_SECONDS = 10

#: Telemetry keys a stage may lift out of a structured child log line and into
#: the Temporal heartbeat. Before this existed a stalled enumeration surfaced as
#: ``lines=2`` with no way to tell which page was slow.
HEARTBEAT_TELEMETRY_KEYS: tuple[str, ...] = (
    "page",
    "page_start",
    "page_end",
    "pages_done",
    "pages_total",
    "pages_cached",
    "items",
    "retry_count",
    "page_elapsed_seconds",
)


async def terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = PROCESS_GROUP_GRACE_SECONDS,
) -> None:
    """Terminate an activity subprocess and every descendant in its process group."""

    if process.returncode is not None:
        return
    pgid = os.getpgid(process.pid)
    os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        os.killpg(pgid, signal.SIGKILL)
        await process.wait()


def stage_log_path(root: Path, receipt_name: str) -> Path:
    """Durable child-stdout location, alongside the run's receipts."""

    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{receipt_name}.stdout.log"


def merge_telemetry(line: str, telemetry: dict[str, Any]) -> dict[str, Any]:
    """Lift whitelisted fields out of a structured child line.

    Non-JSON output is normal (tracebacks, plain prints) and is ignored here; it
    is still preserved verbatim in the durable stdout log.
    """

    stripped = line.strip()
    if not stripped.startswith("{"):
        return telemetry
    try:
        payload = json.loads(stripped)
    except ValueError:
        return telemetry
    if not isinstance(payload, dict):
        return telemetry
    for key in HEARTBEAT_TELEMETRY_KEYS:
        if key in payload:
            telemetry[key] = payload[key]
    event = payload.get("event")
    if isinstance(event, str):
        telemetry["event"] = event
    return telemetry


async def run_subprocess_with_heartbeat(
    command: Sequence[str],
    *,
    cwd: str,
    heartbeat: Callable[[dict[str, Any]], None],
    stage: str,
    log_path: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run a stage subprocess, preserving its stdout and enriching heartbeats."""

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    started = monotonic()
    line_count = 0
    telemetry: dict[str, Any] = {"stage": stage, "state": "running"}
    assert process.stdout is not None

    def emit() -> None:
        heartbeat(
            {
                **telemetry,
                "lines": line_count,
                "elapsed_seconds": round(monotonic() - started, 3),
            }
        )

    with contextlib.ExitStack() as stack:
        sink = (
            stack.enter_context(log_path.open("a", encoding="utf-8"))
            if log_path is not None
            else None
        )
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    emit()
                    continue
                if not raw:
                    break
                line_count += 1
                line = raw.decode("utf-8", errors="replace")
                if sink is not None:
                    sink.write(line if line.endswith("\n") else line + "\n")
                    sink.flush()
                merge_telemetry(line, telemetry)
                emit()
            return await process.wait()
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
