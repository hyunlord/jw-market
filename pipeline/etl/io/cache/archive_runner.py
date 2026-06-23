from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .archive_cd_display_patch import apply_cd_display_patch
from .archive_target_4bucket_patch import apply_target_4bucket_patch

ROOT = Path(__file__).resolve().parents[4]
ARCHIVE_REF = "99a308b4c42c823870ea52868c0c8f9e1f1facb5"
LAYER3_SHIM_PATH = "pipeline/scripts/etl/layer3_compute_general_v3.py"
SERVICES_SHIM_PATH = "pipeline/etl/io/cache/archive_services_shim.py"
MaterializeMode = Literal["git-show", "vendored"]


@dataclass(frozen=True, slots=True)
class BuilderResult:
    script: str
    rc: int


ARCHIVE_PATHS = (
    "pipeline/scripts/etl/cache_build_common.py",
    "pipeline/scripts/etl/build_cache_brands.py",
    "pipeline/scripts/etl/build_cache_market_status.py",
    "pipeline/scripts/etl/build_cache_cause.py",
    "pipeline/scripts/etl/build_cache_deep_analysis.py",
    "pipeline/scripts/etl/phase29_events.py",
    "pipeline/scripts/etl/iron_iv_dimensions.py",
    "pipeline/scripts/etl/ubist_channel_resolver.py",
    "pipeline/scripts/api/metadata/__init__.py",
    "pipeline/scripts/api/metadata/ml_market_meta.py",
    "pipeline/scripts/utils/ubist_channel_mapping.py",
    "pipeline/scripts/forecast/forecast_runner.py",
    "pipeline/scripts/forecast/backtest.py",
    "pipeline/scripts/forecast/sarima_runner.py",
    "pipeline/scripts/forecast/sentiment_scorer.py",
)


def _git_show(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{ARCHIVE_REF}:{path}"], cwd=ROOT)


def _write_archive_file(temp_root: Path, path: str) -> None:
    destination = temp_root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_git_show(path))


def _copy_vendored_file(temp_root: Path, path: str) -> None:
    destination = temp_root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / path, destination)


def _write_shims(temp_root: Path) -> None:
    etl_dir = temp_root / "pipeline" / "scripts" / "etl"
    etl_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / LAYER3_SHIM_PATH, etl_dir / "layer3_compute_general_v3.py")
    services = temp_root / "pipeline" / "scripts" / "api" / "services.py"
    services.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / SERVICES_SHIM_PATH, services)


def materialize_archive(mode: MaterializeMode = "git-show") -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="jw-s6-archive-"))
    for path in ARCHIVE_PATHS:
        match mode:
            case "git-show":
                _write_archive_file(temp_root, path)
            case "vendored":
                _copy_vendored_file(temp_root, path)
    _write_shims(temp_root)
    output_link = temp_root / "output"
    if output_link.exists():
        output_link.unlink()
    output_link.symlink_to(ROOT / "output", target_is_directory=True)
    parquet_link = temp_root / "parquet"
    if parquet_link.exists():
        parquet_link.unlink()
    parquet_link.symlink_to(ROOT / "parquet", target_is_directory=True)
    apply_target_4bucket_patch(temp_root, ROOT)
    apply_cd_display_patch(temp_root, ROOT)
    return temp_root


def materialize_vendored() -> Path:
    return materialize_archive(mode="vendored")


def build_env(target_db: str) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("MARIADB_HOST", "127.0.0.1")
    env.setdefault("MARIADB_PORT", "3308")
    env["MARIADB_USER"] = os.environ.get("CACHE_MARIADB_USER", "root")
    password = env.get("MARIADB_ROOT_PASSWORD") or env.get("MYSQL_PWD") or env.get("MARIADB_PASSWORD")
    if password:
        env["MARIADB_PASSWORD"] = password
    env["MARIADB_DATABASE"] = target_db
    return env


def _run_script(temp_root: Path, script: str, args: list[str], env: dict[str, str]) -> BuilderResult:
    etl_dir = temp_root / "pipeline" / "scripts" / "etl"
    command = [sys.executable, str(etl_dir / script), *args]
    completed = subprocess.run(command, cwd=etl_dir, env=env, check=False)
    return BuilderResult(script=script, rc=int(completed.returncode))


def run_archive_builders(
    target_db: str,
    *,
    smoke_market: str | None = None,
    cache_cause_mode: str = "full-all-brands",
    mode: MaterializeMode = "git-show",
) -> list[BuilderResult]:
    temp_root = materialize_archive(mode=mode)
    try:
        env = build_env(target_db)
        results = [
            _run_script(temp_root, "build_cache_brands.py", ["--verbose"], env),
            _run_script(temp_root, "build_cache_market_status.py", ["--verbose"], env),
        ]
        cause_args = ["--verbose"]
        if cache_cause_mode == "full-all-brands":
            cause_args.append("--full-all-brands")
        elif cache_cause_mode != "serving-slim":
            raise ValueError(f"unsupported cache_cause_mode: {cache_cause_mode}")
        if smoke_market:
            cause_args.extend(["--market", smoke_market])
        results.append(_run_script(temp_root, "build_cache_cause.py", cause_args, env))
        if not smoke_market:
            results.append(_run_script(temp_root, "build_cache_deep_analysis.py", ["--verbose"], env))
        return results
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def run_vendored_builders(
    target_db: str,
    *,
    smoke_market: str | None = None,
    cache_cause_mode: str = "full-all-brands",
) -> list[BuilderResult]:
    return run_archive_builders(
        target_db,
        smoke_market=smoke_market,
        cache_cause_mode=cache_cause_mode,
        mode="vendored",
    )
