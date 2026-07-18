"""Fail-closed full-pipeline rehearsal against isolated schemas.

The regular orchestrator starts after mart refresh.  This module is a separate
entrypoint for R-1: it materializes explicit raw inputs, rebuilds an isolated
mart, and creates caches in a second isolated schema.  It never publishes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PY = sys.executable or "python3"
SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_]+$")
MART_PREFIX = "jw_mart_rehearsal_"
CACHE_PREFIX = "jw_mart_s6_rehearsal_"
PROTECTED_DATABASES = {"jw_mart", "jw_mart_d2_stage_20260630_r2"}


class RehearsalContractError(ValueError):
    """Raised before execution when an R-1 isolation contract is incomplete."""


@dataclass(frozen=True)
class FullInputManifest:
    ubist_source_dir: Path
    iqvia_source_dir: Path
    mi_master: Path


@dataclass(frozen=True)
class FullRehearsalConfig:
    input_manifest: Path
    target_db: str
    cache_db: str
    source_db: str
    work_dir: Path

    def validate(self) -> FullInputManifest:
        for label, value, prefix in (
            ("target_db", self.target_db, MART_PREFIX),
            ("cache_db", self.cache_db, CACHE_PREFIX),
        ):
            if not SAFE_DB_RE.fullmatch(value) or not value.startswith(prefix):
                raise RehearsalContractError(f"{label} must start with {prefix!r}: {value!r}")
            if value in PROTECTED_DATABASES or value == self.source_db:
                raise RehearsalContractError(f"{label} is not isolated: {value!r}")
        if self.target_db == self.cache_db:
            raise RehearsalContractError("target_db and cache_db must be separate schemas")
        if not SAFE_DB_RE.fullmatch(self.source_db):
            raise RehearsalContractError(f"unsafe source_db: {self.source_db!r}")
        return load_input_manifest(self.input_manifest)


@dataclass(frozen=True)
class RehearsalStep:
    key: str
    argv: tuple[str, ...]
    writes_operating: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "argv": list(self.argv),
            "writes_operating": self.writes_operating,
        }


def _required_path(payload: dict[str, object], key: str, base: Path) -> Path:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise RehearsalContractError(f"input manifest requires {key}")
    path = Path(raw)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _source_files(root: Path, suffixes: set[str]) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith(("~$", "._"))
        and path.suffix.lower() in suffixes
    )


def load_input_manifest(path: Path) -> FullInputManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalContractError(f"cannot read input manifest {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RehearsalContractError("input manifest schema_version must be 1")

    base = path.resolve().parent
    ubist = _required_path(payload, "ubist_source_dir", base)
    iqvia = _required_path(payload, "iqvia_source_dir", base)
    master = _required_path(payload, "mi_master", base)
    if not _source_files(ubist, {".xlsx"}):
        raise RehearsalContractError(f"no UBIST source files under {ubist}")
    if not _source_files(iqvia, {".csv", ".xls", ".xlsx"}):
        raise RehearsalContractError(f"no IQVIA source files under {iqvia}")
    if not master.is_file() or master.suffix.lower() != ".xlsx":
        raise RehearsalContractError(f"MI Master workbook is missing or not xlsx: {master}")
    return FullInputManifest(ubist, iqvia, master)


def _etl(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.etl.run", *args)


def build_full_rehearsal_plan(config: FullRehearsalConfig) -> tuple[RehearsalStep, ...]:
    inputs = config.validate()
    work = config.work_dir.resolve()
    ubist_parquet = work / "ubist"
    iqvia_records = work / "iqvia-records"
    iqvia_nsa = work / "iqvia-nsa"
    catalog_root = work / "output" / "catalog"
    enriched = work / "enriched"
    common = ("--target-db", config.target_db)

    return (
        RehearsalStep(
            "load_ubist",
            _etl(
                "--stage", "s1", "--source", "ubist",
                "--ubist-source-dir", str(inputs.ubist_source_dir),
                "--target-dir", str(ubist_parquet),
                "--ubist-mode", "replace",
            ),
        ),
        RehearsalStep(
            "load_iqvia",
            _etl(
                "--stage", "s1", "--source", "iqvia",
                "--iqvia-source-dir", str(inputs.iqvia_source_dir),
                *common,
                "--source-db", config.source_db,
                "--record-parquet-dir", str(iqvia_records),
                "--iqvia-nsa-dir", str(iqvia_nsa),
            ),
        ),
        RehearsalStep(
            "catalog",
            _etl(
                "--stage", "s2", "--mi-master", str(inputs.mi_master),
                "--target-dir", str(work), "--ubist-dir", str(ubist_parquet),
                "--iqvia-nsa-dir", str(iqvia_nsa),
                "--catalog-root", str(catalog_root), "--sync-catalog-db", *common,
            ),
        ),
        RehearsalStep(
            "enrich",
            _etl(
                "--stage", "s3", "--target-dir", str(enriched),
                "--ubist-dir", str(ubist_parquet), "--iqvia-nsa-dir", str(iqvia_nsa),
                "--catalog-root", str(catalog_root),
            ),
        ),
        RehearsalStep(
            "general_mart",
            _etl(
                "--stage", "s4", *common, "--source-db", config.target_db,
                "--ubist-dir", str(ubist_parquet), "--catalog-root", str(catalog_root),
            ),
        ),
        RehearsalStep(
            "strategic_mart",
            _etl(
                "--stage", "s5", *common, "--source-db", config.target_db,
                "--catalog-root", str(catalog_root),
            ),
        ),
        RehearsalStep(
            "bridge",
            _etl(
                "--stage", "s7", *common, "--source-db", config.target_db,
                "--catalog-root", str(catalog_root),
            ),
        ),
        RehearsalStep(
            "cache",
            _etl(
                "--stage", "s6", "--target-db", config.cache_db,
                "--source-db", config.target_db,
                "--strategic-source-db", config.target_db,
                "--event-source-db", config.source_db,
            ),
        ),
    )


def execute_full_rehearsal(config: FullRehearsalConfig, *, dry_run: bool) -> int:
    plan = build_full_rehearsal_plan(config)
    if dry_run:
        print(json.dumps([step.as_dict() for step in plan], ensure_ascii=False, indent=2))
        return 0

    config.work_dir.mkdir(parents=True, exist_ok=False)
    for step in plan:
        result = subprocess.run(step.argv, check=False)
        if result.returncode != 0:
            print(f"rehearsal failed step={step.key} rc={result.returncode}", file=sys.stderr)
            return int(result.returncode or 1)
    return 0
