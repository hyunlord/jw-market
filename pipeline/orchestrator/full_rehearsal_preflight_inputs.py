"""Bounded source probes used by the R-1 preflight."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from pipeline.orchestrator.full_rehearsal import FullInputManifest, FullRehearsalConfig


def bounded_input_failures(inputs: FullInputManifest) -> tuple[int, list[str]]:
    failures: list[str] = []
    source_files = [
        path
        for root in (inputs.ubist_source_dir, inputs.iqvia_source_dir)
        for path in root.rglob("*")
        if path.is_file()
    ] + [inputs.mi_master]
    for path in source_files:
        suffix = path.suffix.lower()
        if suffix not in {".xlsx", ".xls", ".csv"}:
            failures.append(f"unsupported suffix: {path.name}")
        elif path.stat().st_size == 0:
            failures.append(f"zero-byte input: {path.name}")
        elif suffix == ".xlsx" and not zipfile.is_zipfile(path):
            failures.append(f"invalid xlsx container: {path.name}")
        elif suffix == ".xls" and path.read_bytes()[:8] != bytes.fromhex(
            "D0CF11E0A1B11AE1"
        ):
            failures.append(f"invalid xls header: {path.name}")
        elif suffix == ".csv":
            sample = path.read_bytes()[:65536]
            if not any(delimiter in sample for delimiter in (b",", b"\t", b"|")):
                failures.append(f"unparseable csv header: {path.name}")
    return len(source_files), failures


def capacity_failures(
    config: FullRehearsalConfig,
    inputs: FullInputManifest,
    evidence_dir: Path,
) -> tuple[int, list[str]]:
    parent = config.work_dir.parent
    failures = []
    if not parent.is_dir():
        failures.append("work_dir parent is missing")
        free = 0
    else:
        stats = os.statvfs(parent)
        free = stats.f_bavail * stats.f_frsize
    if not evidence_dir.is_dir() or not os.access(evidence_dir, os.W_OK):
        failures.append("durable evidence directory is missing or not writable")
    source_size = sum(
        path.stat().st_size
        for root in (inputs.ubist_source_dir, inputs.iqvia_source_dir)
        for path in root.rglob("*")
        if path.is_file()
    )
    if config.work_dir.exists():
        failures.append("work_dir already exists")
    if free < source_size * 2:
        failures.append("free space is below 2x raw input size")
    return source_size, failures
