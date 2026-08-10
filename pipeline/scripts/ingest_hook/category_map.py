"""Declarative category -> (G3 expectations, load command, refresh scope) map.

One row per contract category. When the contract document pins the real
category list, update THIS table only; g3/job_runner/launcher read it.

Command layers (see D_design.txt D-2):
  * ``load_argv``      — system A, file -> staging -> mart (``pipeline.etl.run``).
                         Empty tuple = no load phase for the category.
  * ``refresh_argv``   — system B, downstream numeric refresh. Ingest calls use
                         ``--force`` because manifest replacement can change
                         data without changing the epoch.
Unknown categories fail closed everywhere (STOP ③: no path around G3).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum

PY = sys.executable or "python3"

# Row-count crash floor: current total < floor * previous completed total => G3 fail.
DEFAULT_ROW_FLOOR_RATIO = 0.5


class ActivationKind(StrEnum):
    """Serving activation family; unsupported sources remain explicitly inert."""

    NONE = "none"
    UBIST_NUMERIC = "ubist_numeric"
    IQVIA_NSA = "iqvia_nsa"
    CSD_CHANNEL = "csd_channel"
    CSD_KEYWORD = "csd_keyword"


CSD_CHANNEL_E2E_STAGES: tuple[tuple[str, str], ...] = (
    ("g3", "G3"),
    ("load", "적재"),
    ("mart_publish", "CSD 원천·스테이지 게시"),
    ("context_bridge", "컨텍스트 브리지"),
    ("dashboard", "대시보드"),
)

CSD_KEYWORD_E2E_STAGES: tuple[tuple[str, str], ...] = (
    ("g3", "G3"),
    ("load", "적재"),
    ("mart_publish", "CSD 원천·스테이지 게시"),
    ("topic_extraction", "토픽 배정"),
    ("dashboard", "대시보드"),
)


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
    load_epoch_flag: str | None = None
    load_verify: str | None = None
    # G4 — workbook (.xlsx) structural validation reader. None = the workbook's
    # sheet schema is gated downstream (e.g. mimaster -> s2 catalog), so G3 pins
    # identity only. A non-None value names a reader G3 uses to validate the
    # workbook structure BEFORE load, reusing the loader's own parsing contract
    # so G3 and the loader can never diverge (one contract, not two). "ubist"
    # reuses ubist_loader.summarize_source (header-area only, fast on 80MB files).
    workbook_reader: str | None = None
    # True when all files must be passed to one loader invocation so its row
    # counts describe the submission atomically rather than one workbook.
    load_batch_files: bool = False
    # New table adapters are deliberately staging-only until a separate PL gate
    # provisions production schemas and enables mart refresh.
    production_load_supported: bool = True
    activation_kind: ActivationKind = ActivationKind.NONE


def _etl(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.etl.run", *args)


def _orchestrator(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.orchestrator", "run", "--mode", "incremental", *args)


def _category_table_load(category: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.scripts.ingest_hook.category_table_load", "--category", category)


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
        refresh_argv=_orchestrator("--profile", "numeric", "--force"),
        sigma_source="ubist",
        load_input_flag="--file",
        load_target_flag="--target-dir",
        load_verify="ubist_parquet_manifest",
        # G4: the real UBIST submission is a wide .xlsx (2-row header, month
        # columns) loaded via --stage s1 (s2 catalog never runs on this path),
        # so G3 must validate the workbook itself using the loader's parser.
        workbook_reader="ubist",
        load_batch_files=True,
        activation_kind=ActivationKind.UBIST_NUMERIC,
    ),
    CategorySpec(
        key="iqvia_nsa", description="IQVIA NSA quarterly workbook",
        required_columns=(), period_column=None,
        load_argv=_category_table_load("iqvia_nsa"),
        refresh_argv=_orchestrator("--profile", "numeric", "--force"),
        sigma_source="iqvia_nsa", load_input_flag="--file",
        load_target_flag="--target-dir", load_epoch_flag="--epoch",
        load_verify="table_manifest", workbook_reader="iqvia_nsa",
        load_batch_files=True, production_load_supported=True,
        activation_kind=ActivationKind.IQVIA_NSA,
    ),
    CategorySpec(
        key="iqvia_csd_channel", description="IQVIA CSD channel dynamics workbook",
        required_columns=(), period_column=None,
        load_argv=_category_table_load("iqvia_csd_channel"), refresh_argv=(),
        load_input_flag="--file", load_target_flag="--target-dir",
        load_epoch_flag="--epoch", load_verify="table_manifest",
        workbook_reader="iqvia_csd_channel", load_batch_files=True,
        production_load_supported=True,
        activation_kind=ActivationKind.CSD_CHANNEL,
    ),
    CategorySpec(
        key="iqvia_csd_keyword", description="IQVIA CSD keyword workbook",
        required_columns=(), period_column=None,
        load_argv=_category_table_load("iqvia_csd_keyword"), refresh_argv=(),
        load_input_flag="--file", load_target_flag="--target-dir",
        load_epoch_flag="--epoch", load_verify="table_manifest",
        workbook_reader="iqvia_csd_keyword", load_batch_files=True,
        production_load_supported=True,
        activation_kind=ActivationKind.CSD_KEYWORD,
    ),
    CategorySpec(
        key="mi_master", description="MI Master workbook resubmission",
        required_columns=(), period_column=None,
        load_argv=_category_table_load("mi_master"),
        refresh_argv=_orchestrator("--profile", "numeric", "--force"),
        load_input_flag="--file", load_target_flag="--target-dir",
        load_epoch_flag="--epoch", load_verify="table_manifest",
        workbook_reader="mi_master",
        load_batch_files=True, production_load_supported=False,
    ),
    CategorySpec(
        key="skeleton",
        description="Target-priority skeleton refresh (downstream refresh only)",
        required_columns=(),
        period_column=None,
        load_argv=(),
        refresh_argv=_orchestrator("--profile", "numeric", "--force"),
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
