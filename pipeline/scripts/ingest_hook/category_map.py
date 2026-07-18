"""Declarative category -> (G3 expectations, load command, refresh scope) map.

One row per contract category. When the contract document pins the real
category list, update THIS table only; g3/job_runner/launcher read it.

Command layers (see D_design.txt D-2):
  * ``load_argv``      — system A, file -> staging -> mart (``pipeline.etl.run``).
                         Empty tuple = no load phase for the category.
  * ``refresh_argv``   — system B, downstream incremental refresh
                         (``pipeline.orchestrator run --mode incremental``);
                         the orchestrator itself no-ops fresh stages by epoch,
                         so re-invocation is idempotent by design.
Unknown categories fail closed everywhere (STOP ③: no path around G3).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

PY = sys.executable or "python3"

# Row-count crash floor: current total < floor * previous completed total => G3 fail.
DEFAULT_ROW_FLOOR_RATIO = 0.5


@dataclass(frozen=True)
class CategorySpec:
    key: str
    description: str
    # G3 schema expectation: required header columns per data file (csv/xlsx).
    required_columns: tuple[str, ...]
    # Column whose distinct values must contain the manifest epoch (period
    # consistency). None = fall back to files[].period_start/end containment.
    period_column: str | None
    # System A load phase base argv. The uploaded files and the output target
    # are injected per-run by job_runner (load_input_flag / load_target_flag);
    # this tuple carries only the source/mode selection.
    load_argv: tuple[str, ...]
    # System B downstream refresh argv.
    refresh_argv: tuple[str, ...]
    row_floor_ratio: float = DEFAULT_ROW_FLOOR_RATIO
    # mart source key for the post-load Σ(brands)=market reconciliation
    # (sigma_market.check_market_sigma); None = no market sigma for the category.
    sigma_source: str | None = None
    # J5 wiring — how the materialized upload reaches the loader:
    #   load_input_flag  : CLI flag repeated once per uploaded file (e.g. "--file").
    #   load_target_flag : CLI flag for the isolated parquet output dir (e.g. "--target-dir").
    #   load_verify      : load_verify.verify_epoch_loaded kind confirming the
    #                      uploaded epoch actually landed (silent-failure gate).
    # A category with a non-empty load_argv but no load_input_flag is UNWIRED:
    # job_runner fails it closed in real mode rather than load unrelated defaults.
    load_input_flag: str | None = None
    load_target_flag: str | None = None
    load_verify: str | None = None


def _etl(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.etl.run", *args)


def _orchestrator(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.orchestrator", "run", "--mode", "incremental", *args)


CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec(
        key="ubist",
        description="UBIST monthly submission (incremental append; dedup in frame loader)",
        required_columns=("period", "brand", "value"),
        period_column="period",
        # --stage s1 loads the uploaded file's parquet in isolation: it bypasses
        # s0 verify (which requires the full four-source tree) and honors --file
        # (discover_xlsx adds the exact file). The upstream parquet->mart_general_*
        # propagation (s2..s7 / mounted source tree) is a separate stage-orchestration
        # decision flagged to jw agent for D-3; M-2 here proves the staging landing.
        load_argv=_etl("--stage", "s1", "--source", "ubist", "--incremental"),
        refresh_argv=_orchestrator(),
        sigma_source="ubist",
        load_input_flag="--file",
        load_target_flag="--target-dir",
        load_verify="ubist_parquet_manifest",
    ),
    CategorySpec(
        key="iqvia",
        description="IQVIA quarterly submission (staging target-db verification load)",
        required_columns=("period", "brand", "value"),
        period_column="period",
        load_argv=_etl("--source", "iqvia"),
        refresh_argv=_orchestrator(),
        sigma_source="iqvia_nsa",
    ),
    CategorySpec(
        key="mimaster",
        description="MI Master workbook resubmission (catalog rebuild)",
        required_columns=(),  # workbook; sheet-level schema belongs to s2 catalog
        period_column=None,
        load_argv=_etl("--stage", "s2"),
        refresh_argv=_orchestrator(),
    ),
    CategorySpec(
        key="skeleton",
        description="Target-priority skeleton refresh (downstream refresh only)",
        required_columns=(),
        period_column=None,
        load_argv=(),
        refresh_argv=_orchestrator(),
    ),
)

CATEGORY_BY_KEY: dict[str, CategorySpec] = {spec.key: spec for spec in CATEGORIES}


class UnknownCategoryError(ValueError):
    pass


def resolve_category(key: str) -> CategorySpec:
    spec = CATEGORY_BY_KEY.get(key)
    if spec is None:
        raise UnknownCategoryError(
            f"unknown ingest category {key!r}; known: {sorted(CATEGORY_BY_KEY)} (fail-closed)"
        )
    return spec
