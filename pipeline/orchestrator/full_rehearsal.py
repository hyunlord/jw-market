"""Fail-closed full-pipeline rehearsal against isolated schemas.

The regular orchestrator starts after mart refresh.  This module is a separate
entrypoint for R-1: it materializes explicit raw inputs, rebuilds an isolated
mart, and creates caches in a second isolated schema.  It never publishes.
"""

from __future__ import annotations

import hashlib
import json
import os
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
class UbistParquetSidecar:
    path: Path
    relative_path: Path
    sha256: str


@dataclass(frozen=True)
class FullInputManifest:
    ubist_source_dir: Path
    iqvia_source_dir: Path
    mi_master: Path
    ubist_parquet_sidecars: tuple[UbistParquetSidecar, ...] = ()


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
    env: tuple[tuple[str, str], ...] = ()
    writes_operating: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "argv": list(self.argv),
            "env": dict(self.env),
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
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
        raise RehearsalContractError("input manifest schema_version must be 1 or 2")

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
    sidecars = _parse_ubist_parquet_sidecars(payload, base)
    if payload["schema_version"] == 1 and sidecars:
        raise RehearsalContractError("input manifest schema_version 1 cannot contain sidecars")
    return FullInputManifest(ubist, iqvia, master, sidecars)


def _parse_ubist_parquet_sidecars(
    payload: dict[str, object], base: Path
) -> tuple[UbistParquetSidecar, ...]:
    raw_rows = payload.get("ubist_parquet_sidecars", [])
    if not isinstance(raw_rows, list):
        raise RehearsalContractError("ubist_parquet_sidecars must be a list")
    rows: list[UbistParquetSidecar] = []
    destinations: set[Path] = set()
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            raise RehearsalContractError(f"ubist_parquet_sidecars[{index}] must be an object")
        source = _required_path(raw, "path", base)
        relative_raw = raw.get("relative_path")
        sha = raw.get("sha256")
        if not isinstance(relative_raw, str) or not relative_raw.strip():
            raise RehearsalContractError(
                f"ubist_parquet_sidecars[{index}] requires relative_path"
            )
        relative = Path(relative_raw)
        if (
            relative.is_absolute()
            or relative.suffix.lower() != ".parquet"
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RehearsalContractError(f"unsafe UBIST sidecar relative_path: {relative_raw!r}")
        if relative in destinations:
            raise RehearsalContractError(f"duplicate UBIST sidecar relative_path: {relative}")
        destinations.add(relative)
        if not isinstance(sha, str) or len(sha) != 64 or any(
            char not in "0123456789abcdef" for char in sha.lower()
        ):
            raise RehearsalContractError(f"invalid UBIST sidecar SHA256 at index {index}")
        if not source.is_file() or source.suffix.lower() != ".parquet":
            raise RehearsalContractError(f"UBIST sidecar is missing or not parquet: {source}")
        actual_sha = _sha256_file(source)
        if actual_sha != sha.lower():
            raise RehearsalContractError(
                f"UBIST sidecar SHA256 mismatch: expected {sha.lower()}, got {actual_sha}"
            )
        rows.append(UbistParquetSidecar(source, relative, actual_sha))
    return tuple(rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _etl(*args: str) -> tuple[str, ...]:
    return (PY, "-m", "pipeline.etl.run", *args)


def _sidecar_period(relative: Path) -> str:
    """Derive the UBIST period (YYYY-MM) from a sidecar ``relative_path``.

    Sidecar destinations are Hive-partitioned as ``year=YYYY/month=MM/...``.
    """
    year = month = None
    for part in relative.parts:
        if part.startswith("year="):
            year = part[len("year=") :]
        elif part.startswith("month="):
            month = part[len("month=") :]
    if not (year and month and year.isdigit() and month.isdigit()):
        raise RehearsalContractError(
            f"cannot derive UBIST period from sidecar relative_path: {relative}"
        )
    return f"{int(year):04d}-{int(month):02d}"


def build_full_rehearsal_plan(config: FullRehearsalConfig) -> tuple[RehearsalStep, ...]:
    inputs = config.validate()
    work = config.work_dir.resolve()
    ubist_parquet = work / "ubist"
    iqvia_records = work / "iqvia-records"
    iqvia_nsa = work / "iqvia-nsa"
    catalog_root = work / "output" / "catalog"
    enriched = work / "enriched"
    common = ("--target-db", config.target_db)
    api_env = (
        ("BRIDGE_DB_NAME", config.target_db),
        ("DB_NAME", config.target_db),
        ("GENERAL_DIMENSION_DB_NAME", config.target_db),
        ("MALB_TARGET_DB", config.target_db),
        ("MALB_TARGET_TABLE", "mart_analysis_level_block"),
        ("STRATEGIC_DIMENSION_DB_NAME", config.target_db),
    )
    cache_env = (
        ("DB_NAME", config.cache_db),
        ("MARIADB_DATABASE", config.cache_db),
    )

    sidecar_steps = (
        (
            RehearsalStep(
                "install_ubist_sidecars",
                (
                    PY,
                    "-m",
                    "pipeline.orchestrator.full_rehearsal_ubist_sidecars",
                    "--manifest",
                    str(config.input_manifest.resolve()),
                    "--target-dir",
                    str(ubist_parquet),
                ),
            ),
        )
        if inputs.ubist_parquet_sidecars
        else ()
    )

    # s1 must NOT materialize the months pinned to canonical parquet sidecars:
    # otherwise the later install_ubist_sidecars step collides with its
    # no-overwrite guard (the R-1 failure this fixes). Exclude them here so the
    # sidecar step creates those partitions cleanly. Guard stays intact.
    ubist_exclude_args: tuple[str, ...] = tuple(
        arg
        for sidecar in inputs.ubist_parquet_sidecars
        for arg in ("--exclude-ubist-month", _sidecar_period(sidecar.relative_path))
    )

    return (
        RehearsalStep(
            "load_ubist",
            _etl(
                "--stage", "s1", "--source", "ubist",
                "--ubist-source-dir", str(inputs.ubist_source_dir),
                "--target-dir", str(ubist_parquet),
                "--ubist-mode", "replace",
                *ubist_exclude_args,
            ),
        ),
        *sidecar_steps,
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
            "general_dimension",
            (
                PY,
                "-m",
                "pipeline.scripts.etl.build_filter_dimension_metric",
                "--target-db",
                config.target_db,
                "--manifest-path",
                str(work / "general-dimension.json"),
                "--source",
                "all",
                "--ubist-dir",
                str(ubist_parquet),
            ),
            env=(
                ("MARIADB_DATABASE", config.target_db),
                ("MARIADB_SOURCE_DATABASE", config.target_db),
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
            "strategic_dimension",
            (
                PY,
                "-m",
                "pipeline.scripts.etl.build_strategic_filter_dimension_metric",
                "--source-db",
                config.target_db,
                "--target-db",
                config.target_db,
                "--manifest",
                str(work / "strategic-dimension.json"),
                "--replace-table",
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
            "prepare_malb",
            (
                PY,
                "-m",
                "pipeline.orchestrator.full_rehearsal_sidecars",
                "--reference-db",
                config.source_db,
                "--target-db",
                config.target_db,
            ),
        ),
        RehearsalStep(
            "analysis_blocks",
            (PY, "-m", "pipeline.scripts.etl.build_analysis_level_blocks"),
            env=api_env,
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
        RehearsalStep(
            "general_deep_cache",
            (
                PY,
                "-m",
                "pipeline.scripts.etl.build_cache_deep_analysis_general",
                "--verbose",
            ),
            env=cache_env,
        ),
        RehearsalStep(
            "brand_elements_cache",
            (
                PY,
                "-m",
                "pipeline.scripts.etl.cache_brand_elements",
                "--ensure-table",
                "--pilot-fill",
                "--verify",
                "--agent3-schema",
                config.source_db,
            ),
            env=cache_env,
        ),
    )


def execute_full_rehearsal(config: FullRehearsalConfig, *, dry_run: bool) -> int:
    plan = build_full_rehearsal_plan(config)
    if dry_run:
        print(json.dumps([step.as_dict() for step in plan], ensure_ascii=False, indent=2))
        return 0

    config.work_dir.mkdir(parents=True, exist_ok=False)
    for step in plan:
        result = subprocess.run(step.argv, check=False, env={**os.environ, **dict(step.env)})
        if result.returncode != 0:
            print(f"rehearsal failed step={step.key} rc={result.returncode}", file=sys.stderr)
            return int(result.returncode or 1)
    return 0
