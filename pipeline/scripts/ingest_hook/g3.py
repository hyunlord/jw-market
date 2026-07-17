"""G3 structural validation — the mandatory first step of every ingest Job.

Contract v2 checks, in order:
  1. file existence (confined to the input root) and sha256 identity
  2. schema: required header columns per category
  3. period consistency: manifest epoch vs the actual periods inside the files
  4. row sanity: zero rows, declared-vs-actual mismatch, crash vs previous run
  5. dedup: delegated to the frame loader (recorded, not re-implemented here)

G3 opens data files read-only and never writes anything; a failure therefore
leaves zero DB effect (loads only start after G3 passes — STOP ③).
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.scripts.ingest_hook.category_map import CategorySpec
from pipeline.scripts.ingest_hook.contract import Manifest, ManifestFile

_CSV_SUFFIXES = {".csv"}
_WORKBOOK_SUFFIXES = {".xlsx"}


class G3Error(ValueError):
    """Structural validation failed; the load phase must not start."""

    def __init__(self, failures: list[str]):
        self.failures = list(failures)
        super().__init__("; ".join(failures))


@dataclass
class G3Report:
    manifest_sha: str
    epoch: str
    category: str
    file_rows: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    observed_periods: set[str] = field(default_factory=set)

    @property
    def total_rows(self) -> int:
        return sum(self.file_rows.values())


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_confined(input_root: Path, declared: str) -> Path:
    """Resolve a manifest file path while refusing escapes from the root."""
    candidate = (input_root / declared).resolve() if not Path(declared).is_absolute() else Path(declared).resolve()
    root = input_root.resolve()
    if root not in candidate.parents and candidate != root:
        raise G3Error([f"file path escapes input root: {declared!r}"])
    return candidate


def _read_csv_header_and_count(path: Path) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return [], 0
        rows = sum(1 for row in reader if any(cell.strip() for cell in row))
    return [column.strip().lower() for column in header], rows


def _period_values(path: Path, column: str) -> set[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            (row.get(column) or "").strip()
            for row in reader
            if (row.get(column) or "").strip()
        }


def _check_declared_period(entry: ManifestFile, epoch: str, failures: list[str]) -> None:
    if entry.period_start and entry.period_end:
        if not (entry.period_start <= epoch <= entry.period_end):
            failures.append(
                f"{entry.path}: epoch {epoch} outside declared period "
                f"[{entry.period_start}..{entry.period_end}]"
            )


def validate(
    manifest: Manifest,
    spec: CategorySpec,
    input_root: Path,
    *,
    previous_total_rows: int | None = None,
) -> G3Report:
    """Run all G3 checks; raise :class:`G3Error` with every failure collected."""
    report = G3Report(manifest_sha=manifest.manifest_sha, epoch=manifest.epoch, category=manifest.category)
    failures: list[str] = []

    for entry in manifest.files:
        path = _resolve_confined(input_root, entry.path)

        # 1) existence + sha256 identity
        if not path.is_file():
            failures.append(f"{entry.path}: file not found")
            continue
        actual_sha = _hash_file(path)
        if actual_sha != entry.sha256:
            failures.append(f"{entry.path}: sha256 mismatch (manifest {entry.sha256[:12]}…, actual {actual_sha[:12]}…)")
            continue

        suffix = path.suffix.lower()
        if suffix in _WORKBOOK_SUFFIXES:
            # Workbook sheet schemas belong to the s2 catalog gate; G3 pins identity only.
            report.notes.append(f"{entry.path}: workbook content checks delegated to s2 catalog gate")
            _check_declared_period(entry, manifest.epoch, failures)
            continue
        if suffix not in _CSV_SUFFIXES:
            failures.append(f"{entry.path}: unsupported suffix {suffix!r} (contract expects .csv/.xlsx)")
            continue

        # 2) schema
        header, row_count = _read_csv_header_and_count(path)
        missing = [column for column in spec.required_columns if column not in header]
        if missing:
            failures.append(f"{entry.path}: missing required columns {missing} (header={header})")
            continue

        # 3) period consistency (actual file contents vs epoch)
        _check_declared_period(entry, manifest.epoch, failures)
        if spec.period_column:
            periods = _period_values(path, spec.period_column)
            report.observed_periods.update(periods)
            if manifest.epoch not in periods:
                failures.append(
                    f"{entry.path}: epoch {manifest.epoch} absent from {spec.period_column!r} values {sorted(periods)[:6]}"
                )
            future = sorted(value for value in periods if value > manifest.epoch)
            if future:
                failures.append(f"{entry.path}: periods beyond epoch {manifest.epoch}: {future[:6]}")

        # 4) row sanity (per file)
        if row_count == 0:
            failures.append(f"{entry.path}: zero data rows")
        if entry.rows is not None and entry.rows != row_count:
            failures.append(f"{entry.path}: manifest declares rows={entry.rows}, actual {row_count}")
        report.file_rows[entry.path] = row_count

    # 4b) row sanity (crash vs previous completed submission of this category)
    if not failures and previous_total_rows and previous_total_rows > 0:
        floor = int(previous_total_rows * spec.row_floor_ratio)
        if report.total_rows < floor:
            failures.append(
                f"total rows {report.total_rows} below crash floor {floor} "
                f"({spec.row_floor_ratio:.0%} of previous {previous_total_rows})"
            )

    # 5) dedup stays with the frame loader; G3 records the delegation.
    report.notes.append("dedup: delegated to frame loader (automatic on load)")

    if failures:
        raise G3Error(failures)
    return report
