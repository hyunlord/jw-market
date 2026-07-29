"""Bounded source probes used by the R-1 preflight."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import openpyxl

from pipeline.etl.io.iqvia_loader import HeaderContractError, canonicalize_nsa_headers
from pipeline.etl.io.iqvia_numeric import IQVIA_ENRICH_METRICS
from pipeline.orchestrator.full_rehearsal import FullInputManifest, FullRehearsalConfig
from pipeline.orchestrator.iqvia_roles import (
    IqviaRoleContractError,
    bind_iqvia_sources,
    canonical_nsa_source,
)


NSA_SAMPLE_ROWS = 32


def _is_numeric_metric(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    try:
        float(value.replace(",", "").strip())
    except ValueError:
        return False
    return True


def _canonical_nsa_failures(inputs: FullInputManifest) -> list[str]:
    try:
        source = canonical_nsa_source(bind_iqvia_sources(inputs.iqvia_source_dir))
    except IqviaRoleContractError as exc:
        return [f"IQVIA NSA source contract: {exc}"]

    failures: list[str] = []
    workbook = openpyxl.load_workbook(source.path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            try:
                raw_headers = next(rows)
            except StopIteration:
                continue
            try:
                headers = canonicalize_nsa_headers(
                    raw_headers,
                    source=f"{source.relative_path}:{sheet.title}",
                )
            except HeaderContractError as exc:
                failures.append(str(exc))
                continue
            metric_headers = {
                source_header: [
                    index
                    for index, header in enumerate(headers)
                    if header == source_header or header.endswith(f"_{source_header}")
                ]
                for source_header, _ in IQVIA_ENRICH_METRICS
            }
            sampled = 0
            for row_number, values in enumerate(rows, start=2):
                if all(value is None for value in values):
                    continue
                sampled += 1
                for source_header, indexes in metric_headers.items():
                    for index in indexes:
                        value = values[index] if index < len(values) else None
                        if value is not None and not _is_numeric_metric(value):
                            failures.append(
                                "non-numeric IQVIA NSA metric "
                                f"sheet={sheet.title} row={row_number} "
                                f"header={source_header}"
                            )
                if sampled >= NSA_SAMPLE_ROWS:
                    break
    finally:
        workbook.close()
    return failures


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
    failures.extend(_canonical_nsa_failures(inputs))
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
