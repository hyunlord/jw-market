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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.etl.io.source_headers import normalize_source_header
from pipeline.scripts.ingest_hook.category_map import CategorySpec
from pipeline.scripts.ingest_hook.contract import Manifest, ManifestFile

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
    return [normalize_source_header(column) for column in header], rows


def _period_values(path: Path, column: str) -> set[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        header_map = {normalize_source_header(name): name for name in (reader.fieldnames or [])}
        actual = header_map.get(normalize_source_header(column), column)
        return {(row.get(actual) or "").strip() for row in rows if (row.get(actual) or "").strip()}


def _check_declared_period(entry: ManifestFile, epoch: str, failures: list[str]) -> None:
    if entry.period_start and entry.period_end:
        if not (entry.period_start <= epoch <= entry.period_end):
            failures.append(
                f"{entry.path}: epoch {epoch} outside declared period "
                f"[{entry.period_start}..{entry.period_end}]"
            )


def _validate_workbook(
    spec: CategorySpec,
    path: Path,
    entry: ManifestFile,
    epoch: str,
    failures: list[str],
    report: G3Report,
) -> None:
    """Validate content classification, then run exactly one canonical parser.

    Candidate selection is header-bounded. Full row validation is performed
    only by the selected loader contract, avoiding five competing full parses.
    """
    from pipeline.scripts.ingest_hook.workbook_contracts import classify

    try:
        detected = classify(path, epoch)
    except Exception as exc:
        failures.append(f"{entry.path}: workbook category detection failed ({exc})")
        return
    if detected != spec.key:
        failures.append(
            f"{entry.path}: internal structure is {detected}, not declared category {spec.key}"
        )
        return
    if spec.workbook_reader == "ubist":
        _validate_ubist_workbook(path, entry, epoch, failures, report)
        return
    from pipeline.scripts.ingest_hook.workbook_contracts import summarize

    try:
        summary = summarize(spec.workbook_reader or "", path, epoch)
    except Exception as exc:  # noqa: BLE001 - parser rejection is a G3 rejection
        failures.append(f"{entry.path}: {spec.workbook_reader} workbook structure invalid ({exc})")
        return
    if summary.rows <= 0:
        failures.append(f"{entry.path}: zero data rows")
        return
    periods = set(summary.periods)
    report.observed_periods.update(periods)
    if periods and epoch not in periods:
        failures.append(f"{entry.path}: epoch {epoch} absent from workbook periods {sorted(periods)[:6]}")
    future = sorted(value for value in periods if value > epoch)
    if future:
        failures.append(f"{entry.path}: periods beyond epoch {epoch}: {future[:6]}")
    _check_declared_period(entry, epoch, failures)
    if entry.rows is not None and entry.rows != summary.rows:
        failures.append(f"{entry.path}: manifest declares rows={entry.rows}, actual {summary.rows}")
    report.file_rows[entry.path] = summary.rows
    report.notes.append(f"{entry.path}: validated via {spec.workbook_reader} loader contract")


def _validate_ubist_workbook(
    path: Path,
    entry: ManifestFile,
    epoch: str,
    failures: list[str],
    report: G3Report,
) -> None:
    # Heavy deps (openpyxl/pandas/pyarrow) — import only when a workbook is seen.
    from pipeline.etl.io import ubist_loader

    try:
        summary = ubist_loader.summarize_source(path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a structural reject
        # classify_sheet raises RuntimeError("No metric columns ...") on a broken
        # 2-row header; every parser failure is fail-closed with the reason.
        failures.append(f"{entry.path}: UBIST workbook structure invalid ({exc})")
        return

    periods = set(summary.periods)
    if not periods:
        failures.append(f"{entry.path}: no UBIST periods parsed from workbook headers")
        return

    report.observed_periods.update(periods)
    # Period consistency — mirror the CSV path exactly (epoch present, none future).
    if epoch not in periods:
        failures.append(
            f"{entry.path}: epoch {epoch} absent from workbook periods {sorted(periods)[:6]}"
        )
    future = sorted(value for value in periods if value > epoch)
    if future:
        failures.append(f"{entry.path}: periods beyond epoch {epoch}: {future[:6]}")

    _check_declared_period(entry, epoch, failures)

    # Row sanity is manifest-declared for workbooks (streaming every row would
    # blow the header-only budget); record the declared count so the crash floor
    # still has a total, and note that G3 did not stream-verify it.
    if entry.rows is not None:
        report.file_rows[entry.path] = entry.rows
    report.notes.append(
        f"{entry.path}: UBIST workbook validated via loader parser "
        f"(periods={sorted(periods)}; rows manifest-declared, verified on load)"
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

    # 0) set completeness — the manifest's own completeness assertion is enforced
    # AT the gate, not only upstream (webhook 409 / sweep skip). A manifest that
    # reaches the load path with complete=false is a partial set: fail closed
    # rather than load an incomplete submission (contract ① — the set, not a
    # folder scan, defines what is collected; the site guarantees the set).
    if not manifest.complete:
        raise G3Error([
            f"manifest declares complete=false for epoch {manifest.epoch} "
            f"category {manifest.category}: partial submission, refusing to load"
        ])

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

        if zipfile.is_zipfile(path):
            if spec.workbook_reader:
                # G4: validate the workbook structure with the loader's own parser
                # so a valid-sha but structurally-broken workbook cannot pass.
                _validate_workbook(spec, path, entry, manifest.epoch, failures, report)
            else:
                # Categories whose sheet schema is gated downstream (e.g. mimaster
                # -> s2 catalog): G3 pins identity; the delegation is recorded,
                # not a silent skip.
                report.notes.append(
                    f"{entry.path}: workbook sheet schema gated by s2 catalog gate (category {manifest.category})"
                )
                _check_declared_period(entry, manifest.epoch, failures)
            continue

        # Non-ZIP sources are parsed as delimited text by content. A renamed
        # workbook or a fake .xlsx therefore cannot steer category selection.
        if path.suffix.casefold() in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
            failures.append(
                f"{entry.path}: workbook extension does not contain an Office workbook"
            )
            continue
        try:
            header, row_count = _read_csv_header_and_count(path)
        except (OSError, UnicodeError, csv.Error) as exc:
            failures.append(f"{entry.path}: unsupported or corrupt file content ({exc})")
            continue
        missing = [column for column in spec.required_columns if normalize_source_header(column) not in header]
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
