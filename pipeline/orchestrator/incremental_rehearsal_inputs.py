"""Input-boundary helpers for isolated incremental rehearsals."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from pipeline.orchestrator.full_rehearsal import (
    FullInputManifest,
    RehearsalContractError,
    UbistParquetSidecar,
)
from pipeline.scripts.ingest_hook.contract import Manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _submission_source_path(root: Path, relative_raw: str) -> Path:
    relative = Path(relative_raw)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise RehearsalContractError(f"unsafe submission source path: {relative_raw!r}")
    resolved_root = root.resolve()
    candidate = (root / relative).resolve()
    if resolved_root not in candidate.parents:
        raise RehearsalContractError(
            f"submission source path escapes submission source directory: {relative_raw!r}"
        )
    return candidate


def validate_submission_sources(root: Path, submission: Manifest) -> None:
    if not root.is_dir():
        raise RehearsalContractError(f"submission source directory is missing: {root}")
    for entry in submission.files:
        source = _submission_source_path(root, entry.path)
        if not source.is_file():
            raise RehearsalContractError(f"submission source is missing: {source}")
        actual_sha = _sha256(source)
        if actual_sha != entry.sha256:
            raise RehearsalContractError(
                f"submission source SHA256 mismatch for {entry.path!r}: "
                f"expected {entry.sha256}, got {actual_sha}"
            )
        if entry.period_start != submission.epoch or entry.period_end != submission.epoch:
            raise RehearsalContractError(
                f"submission file {entry.path!r} must cover epoch {submission.epoch} exactly"
            )


def sidecar_epoch(sidecar: UbistParquetSidecar) -> str | None:
    values = {
        key: value
        for part in sidecar.relative_path.parts
        if "=" in part
        for key, value in (part.split("=", 1),)
    }
    year = values.get("year")
    month = values.get("month")
    if year is None or month is None or len(year) != 4 or len(month) != 2:
        return None
    if not year.isdigit() or not month.isdigit() or not 1 <= int(month) <= 12:
        return None
    return f"{year}-{month}"


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            _link_or_copy(path, target / path.relative_to(source))


def write_baseline_manifest(
    inputs: FullInputManifest,
    baseline_root: Path,
    sidecars: tuple[UbistParquetSidecar, ...],
) -> tuple[Path, Path]:
    baseline_ubist = baseline_root / "ubist"
    baseline_iqvia = baseline_root / "iqvia"
    _copy_tree(inputs.ubist_source_dir, baseline_ubist)
    _copy_tree(inputs.iqvia_source_dir, baseline_iqvia)
    baseline_master = baseline_root / "mi-master.xlsx"
    _link_or_copy(inputs.mi_master, baseline_master)
    baseline_manifest = baseline_root / "inputs.json"
    payload = {
        "schema_version": 2 if sidecars else 1,
        "ubist_source_dir": str(baseline_ubist),
        "iqvia_source_dir": str(baseline_iqvia),
        "mi_master": str(baseline_master),
    }
    if sidecars:
        payload["ubist_parquet_sidecars"] = [
            {
                "path": str(sidecar.path),
                "relative_path": sidecar.relative_path.as_posix(),
                "sha256": sidecar.sha256,
            }
            for sidecar in sidecars
        ]
    baseline_manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return baseline_manifest, baseline_ubist
