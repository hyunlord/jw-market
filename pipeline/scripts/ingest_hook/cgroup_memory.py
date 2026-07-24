"""Low-overhead cgroup v2 memory sampling for durable ingest stage logs."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CGROUP_ROOT: Final = Path("/sys/fs/cgroup")
SAMPLE_INTERVAL_SECONDS: Final = 1.0


@dataclass(frozen=True, slots=True)
class MemorySample:
    current_bytes: int
    peak_bytes: int


def read_memory_sample(root: Path = CGROUP_ROOT) -> MemorySample | None:
    """Read cgroup v2 memory counters, or return None when unavailable."""
    try:
        current = int((root / "memory.current").read_text(encoding="ascii").strip())
        peak = int((root / "memory.peak").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return MemorySample(current_bytes=current, peak_bytes=peak)


def _stdout(line: str) -> None:
    print(line, flush=True)


def _format_sample(stage: str, kind: str, sample: MemorySample) -> str:
    return (
        f"metric=cgroup_memory stage={stage} sample={kind} "
        f"current_bytes={sample.current_bytes} peak_bytes={sample.peak_bytes}"
    )


@contextmanager
def monitor_cgroup_memory(
    stage: str,
    *,
    root: Path = CGROUP_ROOT,
    interval_seconds: float = SAMPLE_INTERVAL_SECONDS,
    emit: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Emit cgroup samples while a stage runs; stdout is durably teed by the Job."""
    writer = emit or _stdout
    initial = read_memory_sample(root)
    if initial is None:
        writer(f"metric=cgroup_memory stage={stage} status=unavailable")
        yield
        return

    writer(_format_sample(stage, "start", initial))
    stopped = threading.Event()

    def sample_until_stopped() -> None:
        while not stopped.wait(interval_seconds):
            sample = read_memory_sample(root)
            if sample is None:
                writer(f"metric=cgroup_memory stage={stage} status=unavailable")
                return
            writer(_format_sample(stage, "periodic", sample))

    sampler = threading.Thread(
        target=sample_until_stopped,
        name=f"{stage}-cgroup-memory",
        daemon=True,
    )
    sampler.start()
    try:
        yield
    finally:
        stopped.set()
        sampler.join()
        final = read_memory_sample(root)
        if final is not None:
            writer(_format_sample(stage, "end", final))
