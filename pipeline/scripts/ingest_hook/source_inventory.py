"""Content-classified source inventory and period continuity gates.

The scanner uses paths only to constrain the approved operating root. Source
identity always comes from the workbook header classifier.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping

from pipeline.scripts.ingest_hook.workbook_contracts import (
    WorkbookSummary,
    summarize_inventory,
)
from pipeline.scripts.ingest_hook.workbook_source_validation import (
    SourceValidationError,
    detect_workbook_source,
)


PeriodUnit = Literal["month", "quarter"]
FileState = Literal["classified", "excluded", "rejected", "removed"]
GateStatus = Literal["pass", "fail", "warning"]
DEFAULT_INVENTORY_ROOT = Path("/market-output/ingest-file-inventory")
_CATEGORY_PATTERN = re.compile(r"[a-z0-9_]{1,32}")
_EPOCH_PATTERN = re.compile(r"[0-9]{4}-(?:[0-9]{2}|Q[1-4])")
_SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class SourceInventoryError(RuntimeError):
    """The inventory cannot be safely created or published."""


@dataclass(frozen=True, slots=True)
class SourceScanPolicy:
    category: str
    root: Path
    period_unit: PeriodUnit
    excluded_relative_roots: tuple[str, ...] = ()
    rebuild_periods: int | None = None


@dataclass(frozen=True, slots=True)
class FileObservation:
    relative_path: str
    sha256: str
    size: int
    state: FileState
    category: str | None = None
    rows: int | None = None
    periods: tuple[str, ...] = ()
    reason: str | None = None
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    removed_at: str | None = None

    @classmethod
    def removed(
        cls,
        *,
        relative_path: str,
        sha256: str,
        size: int,
        periods: Iterable[str],
        first_observed_at: str | None = None,
        last_observed_at: str | None = None,
        removed_at: str | None = None,
    ) -> "FileObservation":
        return cls(
            relative_path=relative_path,
            sha256=sha256,
            size=size,
            state="removed",
            periods=tuple(sorted(set(periods))),
            reason="absent from current classified Excel snapshot",
            first_observed_at=first_observed_at,
            last_observed_at=last_observed_at,
            removed_at=removed_at,
        )


@dataclass(frozen=True, slots=True)
class ScanSnapshot:
    schema_version: str
    category: str
    epoch: str
    manifest_sha: str
    run_id: str
    observed_at: str
    files: tuple[FileObservation, ...]

    @property
    def classified_count(self) -> int:
        return sum(item.state == "classified" for item in self.files)

    @property
    def excluded_count(self) -> int:
        return sum(item.state == "excluded" for item in self.files)

    @property
    def rejected_count(self) -> int:
        return sum(item.state == "rejected" for item in self.files)

    @property
    def periods(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    period
                    for item in self.files
                    if item.state == "classified"
                    for period in item.periods
                }
            )
        )

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result.update(
            classified_count=self.classified_count,
            excluded_count=self.excluded_count,
            rejected_count=self.rejected_count,
            periods=self.periods,
        )
        return result


@dataclass(frozen=True, slots=True)
class PeriodGate:
    name: str
    status: GateStatus
    periods: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class PeriodGateResult:
    pg4: PeriodGate
    pg5: PeriodGate
    pg6: PeriodGate
    pg7: PeriodGate


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    added_files: tuple[FileObservation, ...]
    changed_files: tuple[FileObservation, ...]
    removed_files: tuple[FileObservation, ...]
    unchanged_files: tuple[FileObservation, ...]

    @property
    def removed_count(self) -> int:
        return len(self.removed_files)


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    snapshot: ScanSnapshot
    snapshot_path: Path
    diff: SnapshotDiff | None
    gates: PeriodGateResult
    rebuild_result: Mapping[str, object]
    commissioning_warnings: tuple[str, ...] = ()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_excluded(relative_path: Path, roots: tuple[str, ...]) -> bool:
    relative_parts = relative_path.parts
    return any(relative_parts[: len(Path(root).parts)] == Path(root).parts for root in roots)


def scan_source(
    policy: SourceScanPolicy,
    *,
    epoch: str,
    manifest_sha: str,
    run_id: str,
    classify: Callable[[Path], str] = detect_workbook_source,
    summarize: Callable[[str, Path, str], WorkbookSummary] = summarize_inventory,
    candidate_files: tuple[tuple[str, Path], ...] | None = None,
    commissioning: bool = False,
    previous: ScanSnapshot | None = None,
) -> ScanSnapshot:
    """Recursively classify one approved source root without filename inference."""
    root = policy.root.resolve()
    if not root.is_dir():
        raise SourceInventoryError(f"source root is not a directory: {root}")
    observations: list[FileObservation] = []
    observed_at = datetime.now(UTC).isoformat()
    candidates = candidate_files or tuple(
        (path.relative_to(root).as_posix(), path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() == ".xlsx"
    )
    previous_by_path = (
        {
            item.relative_path: item
            for item in previous.files
            if item.state != "removed"
        }
        if previous is not None
        else {}
    )
    for relative_text, path in candidates:
        relative = Path(relative_text.replace("!/", "/"))
        resolved_path = path.resolve()
        if "!/" not in relative_text:
            try:
                resolved_path.relative_to(root)
            except ValueError as exc:
                raise SourceInventoryError(
                    f"source workbook escapes approved root: {relative_text}"
                ) from exc
        size = path.stat().st_size
        sha256 = _sha256(path)
        if any(part.startswith("._") for part in relative.parts):
            observations.append(
                FileObservation(
                    relative_path=relative_text,
                    sha256=sha256,
                    size=size,
                    state="excluded",
                    reason="AppleDouble metadata is not an ingest workbook",
                )
            )
            continue
        if not commissioning and _is_excluded(relative, policy.excluded_relative_roots):
            observations.append(
                FileObservation(
                    relative_path=relative_text,
                    sha256=sha256,
                    size=size,
                    state="excluded",
                    reason="outside approved operating population",
                )
            )
            continue
        previous_item = previous_by_path.get(relative_text)
        if previous_item is not None and previous_item.sha256 == sha256:
            observations.append(
                replace(
                    previous_item,
                    size=size,
                    last_observed_at=observed_at,
                    removed_at=None,
                )
            )
            continue
        try:
            detected = classify(path)
        except SourceValidationError as exc:
            observations.append(
                FileObservation(
                    relative_path=relative_text,
                    sha256=sha256,
                    size=size,
                    state="rejected",
                    reason=str(exc),
                )
            )
            continue
        if detected != policy.category:
            observations.append(
                FileObservation(
                    relative_path=relative_text,
                    sha256=sha256,
                    size=size,
                    state="rejected",
                    category=detected,
                    reason=f"content category {detected!r} does not match {policy.category!r}",
                )
            )
            continue
        try:
            summary = summarize(policy.category, path, epoch)
        except (RuntimeError, ValueError, OSError) as exc:
            observations.append(
                FileObservation(
                    relative_path=relative_text,
                    sha256=sha256,
                    size=size,
                    state="rejected",
                    category=detected,
                    reason=f"workbook contract failed: {type(exc).__name__}: {exc}",
                )
            )
            continue
        observations.append(
            FileObservation(
                relative_path=relative_text,
                sha256=sha256,
                size=size,
                state="classified",
                category=detected,
                rows=summary.rows,
                periods=tuple(sorted(summary.periods)),
                reason=summary.detail,
                first_observed_at=observed_at,
                last_observed_at=observed_at,
            )
        )
    return ScanSnapshot(
        schema_version="1",
        category=policy.category,
        epoch=epoch,
        manifest_sha=manifest_sha,
        run_id=run_id,
        observed_at=observed_at,
        files=tuple(observations),
    )


def _period_index(period: str, unit: PeriodUnit) -> int:
    year_text, suffix = period.split("-", 1)
    year = int(year_text)
    if unit == "month":
        month = int(suffix)
        if not 1 <= month <= 12:
            raise SourceInventoryError(f"invalid month period: {period}")
        return year * 12 + month - 1
    if not suffix.startswith("Q"):
        raise SourceInventoryError(f"invalid quarter period: {period}")
    quarter = int(suffix[1:])
    if not 1 <= quarter <= 4:
        raise SourceInventoryError(f"invalid quarter period: {period}")
    return year * 4 + quarter - 1


def _period_from_index(index: int, unit: PeriodUnit) -> str:
    divisor = 12 if unit == "month" else 4
    year, offset = divmod(index, divisor)
    return f"{year:04d}-{offset + 1:02d}" if unit == "month" else f"{year:04d}-Q{offset + 1}"


def internal_period_gaps(periods: Iterable[str], unit: PeriodUnit) -> tuple[str, ...]:
    indexes = sorted({_period_index(period, unit) for period in periods})
    if len(indexes) < 2:
        return ()
    present = set(indexes)
    return tuple(
        _period_from_index(index, unit)
        for index in range(indexes[0], indexes[-1] + 1)
        if index not in present
    )


def evaluate_period_gates(
    *,
    period_unit: PeriodUnit,
    current_periods: set[str],
    previous_periods: set[str],
    removed_files: tuple[FileObservation, ...],
    surviving_file_periods: Mapping[str, set[str]],
    previous_rows: int | None = None,
    current_rows: int | None = None,
) -> PeriodGateResult:
    gaps = internal_period_gaps(current_periods, period_unit)
    pg4 = PeriodGate(
        "PG-4",
        "fail" if gaps else "pass",
        gaps,
        "contractual period window has internal gaps" if gaps else "period window is continuous",
    )
    lost = previous_periods - current_periods
    explained = {period for item in removed_files for period in item.periods}
    still_covered = {period for periods in surviving_file_periods.values() for period in periods}
    unexplained = tuple(
        sorted(
            period
            for period in lost
            if period not in explained or period in still_covered
        )
    )
    pg5 = PeriodGate(
        "PG-5",
        "fail" if unexplained else "pass",
        unexplained,
        (
            "lost periods lack complete removed-file evidence"
            if unexplained
            else "all lost periods are explained"
        ),
    )
    row_drift = (
        previous_rows is not None
        and current_rows is not None
        and previous_rows != current_rows
    )
    previous_newest = max(previous_periods) if previous_periods else None
    current_newest = max(current_periods) if current_periods else None
    newest_drift = previous_newest is not None and previous_newest != current_newest
    return PeriodGateResult(
        pg4=pg4,
        pg5=pg5,
        pg6=PeriodGate(
            "PG-6",
            "warning" if row_drift else "pass",
            (),
            (
                f"classified row count changed from {previous_rows} to {current_rows}"
                if row_drift
                else "classified row count is unchanged or has no prior baseline"
            ),
        ),
        pg7=PeriodGate(
            "PG-7",
            "warning" if newest_drift else "pass",
            (current_newest,) if newest_drift and current_newest is not None else (),
            (
                f"newest content period changed from {previous_newest} to {current_newest}"
                if newest_drift
                else "newest content period is unchanged or has no prior baseline"
            ),
        ),
    )


def mass_deletion_threshold(previous_count: int) -> int:
    if previous_count < 0:
        raise ValueError("previous_count must be non-negative")
    return min(5, max(2, math.ceil(previous_count * 0.20)))


def compare_snapshots(previous: ScanSnapshot, current: ScanSnapshot) -> SnapshotDiff:
    if previous.category != current.category:
        raise SourceInventoryError("cannot compare snapshots from different categories")
    previous_files = {
        item.relative_path: item for item in previous.files if item.state == "classified"
    }
    current_files = {
        item.relative_path: item for item in current.files if item.state == "classified"
    }
    previous_paths = set(previous_files)
    current_paths = set(current_files)
    common = previous_paths & current_paths
    return SnapshotDiff(
        added_files=tuple(current_files[path] for path in sorted(current_paths - previous_paths)),
        changed_files=tuple(
            current_files[path]
            for path in sorted(common)
            if current_files[path].sha256 != previous_files[path].sha256
        ),
        removed_files=tuple(
            FileObservation.removed(
                relative_path=previous_files[path].relative_path,
                sha256=previous_files[path].sha256,
                size=previous_files[path].size,
                periods=previous_files[path].periods,
                first_observed_at=previous_files[path].first_observed_at,
                last_observed_at=previous_files[path].last_observed_at,
                removed_at=current.observed_at,
            )
            for path in sorted(previous_paths - current_paths)
        ),
        unchanged_files=tuple(
            current_files[path]
            for path in sorted(common)
            if current_files[path].sha256 == previous_files[path].sha256
        ),
    )


def enforce_scan_gates(
    previous: ScanSnapshot | None,
    current: ScanSnapshot,
    diff: SnapshotDiff | None,
    *,
    period_unit: PeriodUnit | None = None,
    permissive: bool = False,
) -> PeriodGateResult:
    """Apply approved fail-closed deletion and period gates before any load."""
    if current.rejected_count and not permissive:
        raise SourceInventoryError(
            f"{current.category}: rejected operating workbooks={current.rejected_count}"
        )
    if current.classified_count == 0:
        raise SourceInventoryError(f"{current.category}: classified source population is zero")
    if (previous is None) != (diff is None):
        raise ValueError("previous snapshot and diff must be provided together")
    if previous is not None and previous.category != current.category:
        raise SourceInventoryError("cannot gate snapshots from different categories")
    threshold = mass_deletion_threshold(previous.classified_count) if previous else None
    if (
        diff is not None
        and threshold is not None
        and diff.removed_count >= threshold
        and not permissive
    ):
        raise SourceInventoryError(
            f"{current.category}: mass deletion count {diff.removed_count} "
            f"reaches threshold {threshold}"
        )
    previous_periods = set(previous.periods) if previous is not None else set()
    current_periods = set(current.periods)
    if (
        previous_periods
        and max(previous_periods) not in current_periods
        and not permissive
    ):
        raise SourceInventoryError(
            f"{current.category}: newest previous content period "
            f"{max(previous_periods)} disappeared"
        )
    unit = period_unit or (
        "quarter"
        if any("-Q" in period for period in previous_periods | current_periods)
        else "month"
    )
    surviving = {
        item.relative_path: set(item.periods)
        for item in current.files
        if item.state == "classified"
    }
    result = evaluate_period_gates(
        period_unit=unit,
        current_periods=current_periods,
        previous_periods=previous_periods,
        removed_files=diff.removed_files if diff is not None else (),
        surviving_file_periods=surviving,
        previous_rows=(
            sum(item.rows or 0 for item in previous.files if item.state == "classified")
            if previous is not None
            else None
        ),
        current_rows=sum(
            item.rows or 0 for item in current.files if item.state == "classified"
        ),
    )
    failures = [gate for gate in (result.pg4, result.pg5) if gate.status == "fail"]
    if failures and not permissive:
        detail = "; ".join(f"{gate.name}={','.join(gate.periods)}" for gate in failures)
        raise SourceInventoryError(f"{current.category}: source scan gates failed: {detail}")
    return result


def _commissioning_warnings(
    previous: ScanSnapshot | None,
    current: ScanSnapshot,
    diff: SnapshotDiff | None,
    gates: PeriodGateResult,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if current.rejected_count:
        warnings.append(f"rejected operating workbooks={current.rejected_count}")
    if previous is not None and diff is not None:
        threshold = mass_deletion_threshold(previous.classified_count)
        if diff.removed_count >= threshold:
            warnings.append(
                f"mass deletion count {diff.removed_count} reaches threshold {threshold}"
            )
        previous_periods = set(previous.periods)
        current_periods = set(current.periods)
        if previous_periods and max(previous_periods) not in current_periods:
            warnings.append(
                f"newest previous content period {max(previous_periods)} disappeared"
            )
    for gate in (gates.pg4, gates.pg5, gates.pg6, gates.pg7):
        if gate.status != "pass":
            warnings.append(f"{gate.name}={gate.status}: {gate.reason}")
    return tuple(warnings)


def _zip_candidates(root: Path, extraction_root: Path) -> tuple[tuple[str, Path], ...]:
    """Safely materialize XLSX members while retaining archive-relative identity."""
    result: list[tuple[str, Path]] = []
    archives = tuple(
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() == ".zip"
    )
    total_uncompressed = 0
    for archive_index, archive in enumerate(archives):
        archive_relative = archive.relative_to(root).as_posix()
        with zipfile.ZipFile(archive) as handle:
            for member in sorted(handle.infolist(), key=lambda item: item.filename):
                member_path = Path(member.filename)
                if member.is_dir() or member_path.suffix.lower() != ".xlsx":
                    continue
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise SourceInventoryError(
                        f"zip member escapes extraction root: {archive_relative}!/{member.filename}"
                    )
                file_type = (member.external_attr >> 16) & 0o170000
                if file_type == stat.S_IFLNK:
                    raise SourceInventoryError(
                        f"zip member is a symbolic link: {archive_relative}!/{member.filename}"
                    )
                total_uncompressed += member.file_size
                if total_uncompressed > 16 * 1024**3:
                    raise SourceInventoryError("expanded ZIP workbook population exceeds 16 GiB")
                destination = extraction_root / f"archive-{archive_index}" / member_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(member) as source, destination.open("wb") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
                result.append(
                    (f"{archive_relative}!/{member_path.as_posix()}", destination)
                )
    return tuple(result)


def write_inventory_snapshot(snapshot: ScanSnapshot, output_root: Path) -> Path:
    """Atomically publish one immutable run snapshot; never overwrite evidence."""
    destination = inventory_snapshot_path(
        output_root,
        category=snapshot.category,
        epoch=snapshot.epoch,
        manifest_sha=snapshot.manifest_sha,
        run_id=snapshot.run_id,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{snapshot.run_id}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise SourceInventoryError(f"inventory snapshot already exists: {destination}") from exc
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_scan_snapshot(path: Path) -> ScanSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceInventoryError(f"invalid inventory snapshot {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise SourceInventoryError(f"invalid inventory snapshot shape: {path}")
    try:
        files = tuple(
            FileObservation(
                relative_path=str(item["relative_path"]),
                sha256=str(item["sha256"]),
                size=int(item["size"]),
                state=item["state"],
                category=str(item["category"]) if item.get("category") else None,
                rows=int(item["rows"]) if item.get("rows") is not None else None,
                periods=tuple(str(period) for period in item.get("periods", [])),
                reason=str(item["reason"]) if item.get("reason") else None,
                first_observed_at=(
                    str(item["first_observed_at"])
                    if item.get("first_observed_at")
                    else None
                ),
                last_observed_at=(
                    str(item["last_observed_at"])
                    if item.get("last_observed_at")
                    else None
                ),
                removed_at=str(item["removed_at"]) if item.get("removed_at") else None,
            )
            for item in payload["files"]
        )
        return ScanSnapshot(
            schema_version=str(payload["schema_version"]),
            category=str(payload["category"]),
            epoch=str(payload["epoch"]),
            manifest_sha=str(payload["manifest_sha"]),
            run_id=str(payload["run_id"]),
            observed_at=str(payload["observed_at"]),
            files=files,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceInventoryError(f"invalid inventory snapshot fields {path}: {exc}") from exc


def classified_source_paths(
    snapshot: ScanSnapshot,
    source_root: Path,
    *,
    materialized_paths: Mapping[str, Path] | None = None,
) -> tuple[Path, ...]:
    """Return only current content-classified inputs for canonical cache rebuilds."""
    root = source_root.resolve()
    result: list[Path] = []
    for item in snapshot.files:
        if item.state != "classified":
            continue
        if materialized_paths is not None and item.relative_path in materialized_paths:
            path = materialized_paths[item.relative_path].resolve()
            if not path.is_file():
                raise SourceInventoryError(
                    f"materialized source file disappeared: {item.relative_path}"
                )
            result.append(path)
            continue
        path = (root / item.relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SourceInventoryError(
                f"classified path escapes source root: {item.relative_path}"
            ) from exc
        if not path.is_file():
            raise SourceInventoryError(f"classified source file disappeared: {path}")
        result.append(path)
    return tuple(result)


def run_full_scan(
    policy: SourceScanPolicy,
    *,
    epoch: str,
    manifest_sha: str,
    run_id: str,
    output_root: Path,
    rebuild: Callable[[tuple[Path, ...]], Mapping[str, object]],
    previous: ScanSnapshot | None = None,
    classify: Callable[[Path], str] = detect_workbook_source,
    summarize: Callable[[str, Path, str], WorkbookSummary] = summarize_inventory,
    permissive: bool = False,
    bootstrap_files: tuple[Path, ...] | None = None,
    rebuild_all_current: bool = False,
) -> ScanOutcome:
    """Scan, gate, rebuild, then publish one immutable successful inventory.

    The snapshot is deliberately written last. A hard-gate or cache-rebuild
    failure cannot become the next successful comparison anchor.
    """
    root = policy.root.resolve()
    direct_candidates = tuple(
        (path.relative_to(root).as_posix(), path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() == ".xlsx"
    )
    with tempfile.TemporaryDirectory(prefix=f"jw-ingest-zip-{run_id}-") as temporary:
        archive_candidates = _zip_candidates(root, Path(temporary))
        candidates = (*direct_candidates, *archive_candidates)
        current = scan_source(
            policy,
            epoch=epoch,
            manifest_sha=manifest_sha,
            run_id=run_id,
            classify=classify,
            summarize=summarize,
            candidate_files=tuple(sorted(candidates, key=lambda item: item[0])),
            commissioning=permissive,
            previous=previous,
        )
        diff = compare_snapshots(previous, current) if previous is not None else None
        gates = enforce_scan_gates(
            previous,
            current,
            diff,
            period_unit=policy.period_unit,
            permissive=permissive,
        )
        materialized = dict(archive_candidates)
        if rebuild_all_current:
            selected_files = tuple(
                item for item in current.files if item.state == "classified"
            )
        elif previous is None and bootstrap_files is not None:
            bootstrap_resolved = {path.resolve() for path in bootstrap_files}
            selected_files = tuple(
                item
                for item in current.files
                if item.state == "classified"
                and (
                    materialized.get(item.relative_path, root / item.relative_path).resolve()
                    in bootstrap_resolved
                )
            )
        elif diff is not None:
            changed_paths = {
                item.relative_path for item in (*diff.added_files, *diff.changed_files)
            }
            selected_files = tuple(
                item
                for item in current.files
                if item.state == "classified" and item.relative_path in changed_paths
            )
        else:
            selected_files = tuple(
                item for item in current.files if item.state == "classified"
            )
        if policy.rebuild_periods is not None:
            if policy.rebuild_periods < 1:
                raise SourceInventoryError("rebuild_periods must be positive")
            newest = _period_index(epoch, policy.period_unit)
            oldest = newest - policy.rebuild_periods + 1
            selected_files = tuple(
                item
                for item in selected_files
                if any(
                    oldest <= _period_index(period, policy.period_unit) <= newest
                    for period in item.periods
                )
            )
        selected = replace(current, files=selected_files)
        source_paths = classified_source_paths(
            selected,
            policy.root,
            materialized_paths=materialized,
        )
        rebuild_result = rebuild(source_paths)
    if not isinstance(rebuild_result, Mapping):
        raise SourceInventoryError("cache rebuild must return a mapping summary")
    if diff is not None and diff.removed_files:
        previous_by_path = {
            item.relative_path: item
            for item in previous.files
            if item.state == "classified"
        } if previous is not None else {}
        current_files = tuple(
            replace(
                item,
                first_observed_at=(
                    previous_by_path[item.relative_path].first_observed_at
                    or previous_by_path[item.relative_path].last_observed_at
                    or previous.observed_at
                ),
            )
            if item.state == "classified" and item.relative_path in previous_by_path
            else item
            for item in current.files
        )
        current = replace(current, files=(*current_files, *diff.removed_files))
    elif previous is not None:
        previous_by_path = {
            item.relative_path: item
            for item in previous.files
            if item.state == "classified"
        }
        current = replace(
            current,
            files=tuple(
                replace(
                    item,
                    first_observed_at=(
                        previous_by_path[item.relative_path].first_observed_at
                        or previous_by_path[item.relative_path].last_observed_at
                        or previous.observed_at
                    ),
                )
                if item.state == "classified" and item.relative_path in previous_by_path
                else item
                for item in current.files
            ),
        )
    snapshot_path = write_inventory_snapshot(current, output_root)
    warnings = (
        _commissioning_warnings(previous, current, diff, gates)
        if permissive
        else ()
    )
    return ScanOutcome(current, snapshot_path, diff, gates, rebuild_result, warnings)


def inventory_snapshot_path(
    output_root: Path,
    *,
    category: str,
    epoch: str,
    manifest_sha: str,
    run_id: str,
) -> Path:
    values = (
        ("category", category, _CATEGORY_PATTERN),
        ("epoch", epoch, _EPOCH_PATTERN),
        ("manifest_sha", manifest_sha, _SHA256_PATTERN),
        ("run_id", run_id, _RUN_ID_PATTERN),
    )
    for name, value, pattern in values:
        if pattern.fullmatch(value) is None:
            raise SourceInventoryError(f"invalid inventory {name}: {value!r}")
    return output_root / category / epoch / manifest_sha / f"{run_id}.json"


def read_inventory_snapshot(
    output_root: Path,
    *,
    category: str,
    epoch: str,
    manifest_sha: str,
    run_id: str,
) -> dict[str, object]:
    path = inventory_snapshot_path(
        output_root,
        category=category,
        epoch=epoch,
        manifest_sha=manifest_sha,
        run_id=run_id,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceInventoryError(f"invalid inventory snapshot {path}: {exc}") from exc
    expected = {
        "category": category,
        "epoch": epoch,
        "manifest_sha": manifest_sha,
        "run_id": run_id,
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise SourceInventoryError(f"inventory snapshot identity mismatch: {path}")
    return payload
