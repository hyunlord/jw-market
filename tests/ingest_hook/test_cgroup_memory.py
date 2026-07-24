from __future__ import annotations

from pathlib import Path

from pipeline.scripts.ingest_hook.cgroup_memory import (
    MemorySample,
    monitor_cgroup_memory,
    read_memory_sample,
)


def test_read_memory_sample_returns_cgroup_v2_values(tmp_path: Path) -> None:
    (tmp_path / "memory.current").write_text("123\n", encoding="ascii")
    (tmp_path / "memory.peak").write_text("456\n", encoding="ascii")

    assert read_memory_sample(tmp_path) == MemorySample(
        current_bytes=123,
        peak_bytes=456,
    )


def test_read_memory_sample_returns_none_when_counter_is_unavailable(
    tmp_path: Path,
) -> None:
    assert read_memory_sample(tmp_path) is None


def test_monitor_emits_start_and_end_samples_without_persistent_state(
    tmp_path: Path,
) -> None:
    (tmp_path / "memory.current").write_text("1024\n", encoding="ascii")
    (tmp_path / "memory.peak").write_text("2048\n", encoding="ascii")
    lines: list[str] = []

    with monitor_cgroup_memory(
        "mart_build",
        root=tmp_path,
        interval_seconds=60,
        emit=lines.append,
    ):
        (tmp_path / "memory.current").write_text("1536\n", encoding="ascii")
        (tmp_path / "memory.peak").write_text("3072\n", encoding="ascii")

    assert lines == [
        "metric=cgroup_memory stage=mart_build sample=start "
        "current_bytes=1024 peak_bytes=2048",
        "metric=cgroup_memory stage=mart_build sample=end "
        "current_bytes=1536 peak_bytes=3072",
    ]
