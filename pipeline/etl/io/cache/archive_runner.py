from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ARCHIVE_REF = "99a308b4c42c823870ea52868c0c8f9e1f1facb5"


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
)


SHIM = r'''from __future__ import annotations

import json
import math
import os
from typing import Any

import pymysql


def mariadb_connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3308")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("MYSQL_PWD"),
        database=os.environ.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if hasattr(value, "item"):
        return json_ready(value.item())
    return value


def dumps(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
'''

SERVICES_SHIM = r'''from __future__ import annotations

MARKET_STATUS_COMPANY_BY_BRAND = {
    "라베칸": "녹십자",
    "라베칸듀오": "JW중외제약",
    "제이클": "한미약품",
    "가드렛": "엘지화학",
    "가드메트": "유한양행",
    "타발리스": "유한양행",
    "시그마트": "대웅제약",
    "리바로": "일동제약",
    "리바로젯": "종근당",
    "리바로페노": "종근당",
    "리바로하이": "동아에스티",
    "리바로브이": "유한양행",
    "트루패스": "엘지화학",
    "피나스타": "셀트리온제약",
    "제이다트": "한미약품",
    "뉴트로진": "한독",
    "모빌리아": "한미약품",
    "악템라": "한미약품",
    "페린젝트": "일동제약",
    "베노훼럼": "한미약품",
    "헴리브라": "대웅제약",
    "위너프": "한미약품",
    "위너프A+": "한독",
    "엔커버": "삼성바이오에피스",
    "플라주오피": "한독",
}
'''


def _git_show(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{ARCHIVE_REF}:{path}"], cwd=ROOT)


def _write_archive_file(temp_root: Path, path: str) -> None:
    destination = temp_root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_git_show(path))


def materialize_archive() -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="jw-s6-archive-"))
    for path in ARCHIVE_PATHS:
        _write_archive_file(temp_root, path)
    etl_dir = temp_root / "pipeline" / "scripts" / "etl"
    (etl_dir / "layer3_compute_general_v3.py").write_text(SHIM, encoding="utf-8")
    services = temp_root / "pipeline" / "scripts" / "api" / "services.py"
    services.parent.mkdir(parents=True, exist_ok=True)
    services.write_text(SERVICES_SHIM, encoding="utf-8")
    output_link = temp_root / "output"
    if output_link.exists():
        output_link.unlink()
    output_link.symlink_to(ROOT / "output", target_is_directory=True)
    return temp_root


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
) -> list[BuilderResult]:
    temp_root = materialize_archive()
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
