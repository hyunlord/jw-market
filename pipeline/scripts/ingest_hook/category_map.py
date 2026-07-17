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
    # System A load phase argv (without manifest-derived file args).
    load_argv: tuple[str, ...]
    # System B downstream refresh argv.
    refresh_argv: tuple[str, ...]
    row_floor_ratio: float = DEFAULT_ROW_FLOOR_RATIO


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
        load_argv=_etl("--source", "ubist", "--incremental"),
        refresh_argv=_orchestrator(),
    ),
    CategorySpec(
        key="iqvia",
        description="IQVIA quarterly submission (staging target-db verification load)",
        required_columns=("period", "brand", "value"),
        period_column="period",
        load_argv=_etl("--source", "iqvia"),
        refresh_argv=_orchestrator(),
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
