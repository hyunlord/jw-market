from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

_PROCESS_START_TIME = datetime.now(UTC)
_PROCESS_START_MONOTONIC = time.monotonic()
_PROC_STATM = Path("/proc/self/statm")


def _current_rss_bytes() -> int | None:
    try:
        resident_pages = int(_PROC_STATM.read_text(encoding="ascii").split()[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (IndexError, OSError, ValueError):
        return None
    if resident_pages < 0 or page_size <= 0:
        return None
    return resident_pages * page_size


def process_observability() -> dict[str, str | float | int | None]:
    observed_at = datetime.now(UTC)
    return {
        "process_start_time": _PROCESS_START_TIME.isoformat(),
        "process_uptime_seconds": max(0.0, time.monotonic() - _PROCESS_START_MONOTONIC),
        "observed_at": observed_at.isoformat(),
        "current_rss_bytes": _current_rss_bytes(),
    }
