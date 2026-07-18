"""Isolated full-then-incremental rehearsal inputs and refresh plan."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from pipeline.orchestrator.full_rehearsal import (
    CACHE_PREFIX,
    MART_PREFIX,
    SAFE_DB_RE,
    FullRehearsalConfig,
    RehearsalContractError,
    RehearsalStep,
    build_full_rehearsal_plan,
    execute_full_rehearsal,
    load_input_manifest,
)
from pipeline.orchestrator.full_rehearsal_compare import ComparisonConfig, run_comparison
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import Manifest, ManifestFile, load_manifest
from pipeline.scripts.ingest_hook.job_runner import _check_market_sigma, _validate_and_load


@dataclass(frozen=True)
class IncrementalRehearsalConfig:
    full_input_manifest: Path
    submission_manifest: Path
    target_db: str
    cache_db: str
    source_db: str
    reference_db: str
    reference_cache_db: str
    work_dir: Path
    comparison_output: Path

    def validate(self) -> None:
        FullRehearsalConfig(
            input_manifest=self.full_input_manifest,
            target_db=self.target_db,
            cache_db=self.cache_db,
            source_db=self.source_db,
            work_dir=self.work_dir / "build",
        ).validate()
        for label, value, prefix in (
            ("reference_db", self.reference_db, MART_PREFIX),
            ("reference_cache_db", self.reference_cache_db, CACHE_PREFIX),
        ):
            if not SAFE_DB_RE.fullmatch(value) or not value.startswith(prefix):
                raise RehearsalContractError(f"{label} must start with {prefix!r}: {value!r}")
        if self.reference_db == self.target_db or self.reference_cache_db == self.cache_db:
            raise RehearsalContractError("R-1 references must differ from R-2 targets")
        submission = load_manifest(self.submission_manifest)
        if submission.category != "ubist" or not submission.complete:
            raise RehearsalContractError("R-2 requires one complete UBIST submission")


@dataclass(frozen=True)
class HoldoutInput:
    source_path: Path
    relative_path: Path
    manifest_file: ManifestFile


@dataclass(frozen=True)
class PreparedIncrementalInputs:
    baseline_manifest: Path
    baseline_ubist_dir: Path
    holdouts: tuple[HoldoutInput, ...]
    submission: Manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path, *, excluded: set[Path] | None = None) -> None:
    excluded = excluded or set()
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path in excluded:
            continue
        _link_or_copy(path, target / path.relative_to(source))


def _match_holdouts(config: IncrementalRehearsalConfig) -> tuple[HoldoutInput, ...]:
    inputs = load_input_manifest(config.full_input_manifest)
    submission = load_manifest(config.submission_manifest)
    by_sha: dict[str, list[Path]] = {}
    for path in sorted(inputs.ubist_source_dir.rglob("*.xlsx")):
        if path.is_file() and not path.name.startswith(("~$", "._")):
            by_sha.setdefault(_sha256(path), []).append(path)

    matches: list[HoldoutInput] = []
    for entry in submission.files:
        candidates = by_sha.get(entry.sha256, [])
        if len(candidates) != 1:
            raise RehearsalContractError(
                f"submission file {entry.path!r} must match exactly one full input by sha256; "
                f"matches={len(candidates)}"
            )
        source = candidates[0]
        matches.append(
            HoldoutInput(
                source_path=source,
                relative_path=source.relative_to(inputs.ubist_source_dir),
                manifest_file=entry,
            )
        )
    return tuple(matches)


def prepare_incremental_inputs(config: IncrementalRehearsalConfig) -> PreparedIncrementalInputs:
    """Create a hard-linked full input tree with the submitted set held out."""
    config.validate()
    config.work_dir.mkdir(parents=True, exist_ok=False)
    inputs = load_input_manifest(config.full_input_manifest)
    submission = load_manifest(config.submission_manifest)
    holdouts = _match_holdouts(config)
    baseline_root = config.work_dir / "inputs-minus-increment"
    baseline_ubist = baseline_root / "ubist"
    baseline_iqvia = baseline_root / "iqvia"
    _copy_tree(
        inputs.ubist_source_dir,
        baseline_ubist,
        excluded={holdout.source_path for holdout in holdouts},
    )
    _copy_tree(inputs.iqvia_source_dir, baseline_iqvia)
    baseline_master = baseline_root / "mi-master.xlsx"
    _link_or_copy(inputs.mi_master, baseline_master)
    baseline_manifest = baseline_root / "inputs.json"
    baseline_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ubist_source_dir": str(baseline_ubist),
                "iqvia_source_dir": str(baseline_iqvia),
                "mi_master": str(baseline_master),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return PreparedIncrementalInputs(
        baseline_manifest=baseline_manifest,
        baseline_ubist_dir=baseline_ubist,
        holdouts=holdouts,
        submission=submission,
    )


def install_holdout_inputs(prepared: PreparedIncrementalInputs) -> Manifest:
    """Add the validated monthly set to the baseline tree using stable paths."""
    adjusted: list[ManifestFile] = []
    for holdout in prepared.holdouts:
        _link_or_copy(
            holdout.source_path,
            prepared.baseline_ubist_dir / holdout.relative_path,
        )
        entry = holdout.manifest_file
        adjusted.append(
            ManifestFile(
                path=holdout.relative_path.as_posix(),
                sha256=entry.sha256,
                rows=entry.rows,
                period_start=entry.period_start,
                period_end=entry.period_end,
            )
        )
    return Manifest(
        contract_version=prepared.submission.contract_version,
        epoch=prepared.submission.epoch,
        category=prepared.submission.category,
        complete=prepared.submission.complete,
        files=tuple(adjusted),
        submitted_at=prepared.submission.submitted_at,
        uploaded_by=prepared.submission.uploaded_by,
        manifest_path=prepared.submission.manifest_path,
        manifest_sha=prepared.submission.manifest_sha,
        raw=prepared.submission.raw,
    )


def build_incremental_refresh_plan(
    config: IncrementalRehearsalConfig,
    baseline_manifest: Path,
) -> tuple[RehearsalStep, ...]:
    """Reuse the full canonical chain after the source load boundary."""
    full_plan = build_full_rehearsal_plan(
        FullRehearsalConfig(
            input_manifest=baseline_manifest,
            target_db=config.target_db,
            cache_db=config.cache_db,
            source_db=config.source_db,
            work_dir=config.work_dir / "build",
        )
    )
    return tuple(step for step in full_plan if step.key not in {"load_ubist", "load_iqvia"})


def _execute_steps(steps: tuple[RehearsalStep, ...]) -> int:
    for step in steps:
        result = subprocess.run(
            step.argv,
            check=False,
            env={**os.environ, **dict(step.env)},
        )
        if result.returncode != 0:
            print(f"incremental rehearsal failed step={step.key} rc={result.returncode}", file=sys.stderr)
            return int(result.returncode or 1)
    return 0


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def execute_incremental_rehearsal(
    config: IncrementalRehearsalConfig,
    *,
    dry_run: bool,
) -> int:
    """Prove full-minus-submission + exact incremental equals the R-1 full output."""
    config.validate()
    if dry_run:
        print(
            json.dumps(
                {
                    "gate": "R-2",
                    "classification": "isolated-full-then-incremental",
                    "submission_manifest": str(config.submission_manifest),
                    "target_db": config.target_db,
                    "target_cache_db": config.cache_db,
                    "reference_db": config.reference_db,
                    "reference_cache_db": config.reference_cache_db,
                    "writes_operating": False,
                    "phases": [
                        "full-minus-submission",
                        "g3-exact-load",
                        "refresh",
                        "sigma",
                        "exact-comparison",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    prepared = prepare_incremental_inputs(config)
    baseline_config = FullRehearsalConfig(
        input_manifest=prepared.baseline_manifest,
        target_db=config.target_db,
        cache_db=config.cache_db,
        source_db=config.source_db,
        work_dir=config.work_dir / "build",
    )
    rc = execute_full_rehearsal(baseline_config, dry_run=False)
    if rc != 0:
        return rc

    submission = install_holdout_inputs(prepared)
    spec = resolve_category(submission.category)
    refresh_plan = build_incremental_refresh_plan(config, prepared.baseline_manifest)
    environment = {
        "INGEST_UBIST_TARGET_DIR": str((config.work_dir / "build" / "ubist").resolve()),
        "BRIDGE_DB_NAME": config.target_db,
        "DB_NAME": config.target_db,
        "GENERAL_DIMENSION_DB_NAME": config.target_db,
        "MALB_TARGET_DB": config.target_db,
        "MARIADB_DATABASE": config.target_db,
        "MARIADB_SOURCE_DATABASE": config.target_db,
        "STRATEGIC_DIMENSION_DB_NAME": config.target_db,
    }
    with _temporary_environment(environment):
        report = _validate_and_load(
            submission,
            spec,
            prepared.baseline_ubist_dir,
            previous_total_rows=None,
            rehearsal_root=None,
        )
        rc = _execute_steps(refresh_plan)
        if rc != 0:
            return rc
        _check_market_sigma(spec, report)

    return run_comparison(
        ComparisonConfig(
            reference_db=config.reference_db,
            target_db=config.target_db,
            reference_cache_db=config.reference_cache_db,
            target_cache_db=config.cache_db,
        ),
        config.comparison_output,
        gate="R-2",
        environment="isolated-full-then-incremental",
    )
