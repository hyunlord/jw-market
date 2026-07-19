"""Isolated full-then-incremental rehearsal inputs and refresh plan."""
from __future__ import annotations

import json
import os
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
    UbistParquetSidecar,
    build_full_rehearsal_plan,
    execute_full_rehearsal,
    load_input_manifest,
)
from pipeline.orchestrator.full_rehearsal_compare import ComparisonConfig, run_comparison
from pipeline.orchestrator.incremental_rehearsal_inputs import (
    sidecar_epoch,
    validate_input_inventory,
    validate_submission_sources,
    write_baseline_manifest,
)
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import Manifest, load_manifest
from pipeline.scripts.ingest_hook.job_runner import _check_market_sigma, _validate_and_load


@dataclass(frozen=True)
class IncrementalRehearsalConfig:
    full_input_manifest: Path
    full_input_inventory: Path
    expected_input_inventory_sha256: str
    submission_manifest: Path
    submission_source_dir: Path
    target_db: str
    cache_db: str
    source_db: str
    reference_db: str
    reference_cache_db: str
    work_dir: Path
    comparison_output: Path

    def validate(self) -> None:
        validate_input_inventory(
            self.full_input_inventory,
            self.expected_input_inventory_sha256,
        )
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
        validate_submission_sources(self.submission_source_dir, submission)


@dataclass(frozen=True)
class PreparedIncrementalInputs:
    baseline_manifest: Path
    baseline_ubist_dir: Path
    held_out_sidecars: tuple[UbistParquetSidecar, ...]
    submission: Manifest


def prepare_incremental_inputs(config: IncrementalRehearsalConfig) -> PreparedIncrementalInputs:
    """Create a hard-linked full input tree with the submitted epoch held out."""
    config.validate()
    config.work_dir.mkdir(parents=True, exist_ok=False)
    inputs = load_input_manifest(config.full_input_manifest)
    submission = load_manifest(config.submission_manifest)
    held_out_sidecars = tuple(
        sidecar
        for sidecar in inputs.ubist_parquet_sidecars
        if sidecar_epoch(sidecar) == submission.epoch
    )
    if len(held_out_sidecars) != 1:
        raise RehearsalContractError(
            f"full input must contain exactly one {submission.epoch} sidecar for R-2 holdout; "
            f"found {len(held_out_sidecars)}"
        )
    baseline_sidecars = tuple(
        sidecar
        for sidecar in inputs.ubist_parquet_sidecars
        if sidecar_epoch(sidecar) != submission.epoch
    )
    baseline_root = config.work_dir / "inputs-minus-increment"
    baseline_manifest, baseline_ubist = write_baseline_manifest(
        inputs,
        baseline_root,
        baseline_sidecars,
    )
    return PreparedIncrementalInputs(
        baseline_manifest=baseline_manifest,
        baseline_ubist_dir=baseline_ubist,
        held_out_sidecars=held_out_sidecars,
        submission=submission,
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
    return tuple(
        step
        for step in full_plan
        if step.key not in {"load_ubist", "load_iqvia", "install_ubist_sidecars"}
    )


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
                    "input_inventory_sha256": config.expected_input_inventory_sha256,
                    "submission_source_dir": str(config.submission_source_dir),
                    "target_db": config.target_db,
                    "target_cache_db": config.cache_db,
                    "reference_db": config.reference_db,
                    "reference_cache_db": config.reference_cache_db,
                    "writes_operating": False,
                    "phases": [
                        "full-minus-sidecar",
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

    submission = prepared.submission
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
            config.submission_source_dir,
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
        input_inventory_sha256=config.expected_input_inventory_sha256,
    )
