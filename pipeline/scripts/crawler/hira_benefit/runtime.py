from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable, Sequence
from typing import Any

HEARTBEAT_INTERVAL_SECONDS = 30
PROCESS_GROUP_GRACE_SECONDS = 10


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


async def run_subprocess_with_heartbeat(
    command: Sequence[str],
    *,
    cwd: str,
    heartbeat: Callable[[dict[str, Any]], None],
    stage: str,
) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    line_count = 0
    assert process.stdout is not None
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
            except TimeoutError:
                heartbeat({"stage": stage, "state": "running", "lines": line_count})
                continue
            if not raw:
                break
            line_count += 1
            heartbeat({"stage": stage, "state": "running", "lines": line_count})
        return await process.wait()
    except asyncio.CancelledError:
        await terminate_process_group(process)
        raise
