from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
import hashlib
import os
import shutil
import subprocess
import time
from typing import Any, BinaryIO, Mapping

import pymysql

from pipeline.scripts.deploy.mart_load_ops import PROTECTED_TARGETS, _db_env, validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import (
    canonical_reference_digest,
    fetch_group_counts,
    quote_id,
    table_exists,
)


GROUP_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "mart_general_brand_metric": ("source", "measure"),
    "mart_general_market_metric": ("source", "measure"),
    "mart_brand_molecule": ("mart_source",),
    "mart_strategic_ml_market_metric": ("source", "measure"),
}
RESTORE_TRANSACTION_WRAPPER_PREFIXES = (
    b"SET @OLD_AUTOCOMMIT=@@AUTOCOMMIT",
    b"START TRANSACTION",
)
RESTORE_TRANSACTION_WRAPPER_LINES = {
    b"BEGIN;",
    b"COMMIT;",
    b"SET AUTOCOMMIT=@OLD_AUTOCOMMIT;",
}


@dataclass(frozen=True, slots=True)
class RestoreResult:
    path: Path
    size_bytes: int
    elapsed_seconds: float


def capture_manifest(
    conn: pymysql.connections.Connection,
    *,
    run_id: str,
    source_db: str,
    build_db: str,
    tables: tuple[str, ...],
) -> dict[str, Any]:
    entries = []
    for table in tables:
        digest = canonical_reference_digest(conn, build_db, table)
        groups = _render_groups(fetch_group_counts(conn, build_db, table, GROUP_COLUMNS.get(table, ())))
        entries.append(
            {
                "table": table,
                "row_count": digest.row_count,
                "canonical_sha256": digest.sha256,
                "groups": groups,
            }
        )
    return {"run_id": run_id, "source_db": source_db, "build_db": build_db, "tables": entries}


def attach_dump_to_manifest(manifest: dict[str, Any], *, dump_path: Path, dump_seconds: float) -> dict[str, Any]:
    manifest["dump"] = {
        "path": str(dump_path),
        "size_bytes": dump_path.stat().st_size,
        "sha256": sha256_file(dump_path),
        "elapsed_seconds": round(dump_seconds, 3),
    }
    return manifest


def verify_against_manifest(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    results = []
    for entry in manifest_entries(manifest):
        table = str(entry["table"])
        if not table_exists(conn, target_db, table):
            raise RuntimeError(f"Missing imported table: {target_db}.{table}")
        digest = canonical_reference_digest(conn, target_db, table)
        expected_rows = int(entry["row_count"])
        expected_sha = str(entry["canonical_sha256"])
        if digest.row_count != expected_rows:
            raise RuntimeError(f"{target_db}.{table} row count mismatch: expected={expected_rows} actual={digest.row_count}")
        if digest.sha256 != expected_sha:
            raise RuntimeError(
                f"{target_db}.{table} canonical checksum mismatch: expected={expected_sha} actual={digest.sha256}"
            )
        groups = _render_groups(fetch_group_counts(conn, target_db, table, GROUP_COLUMNS.get(table, ())))
        expected_groups = dict(entry.get("groups") or {})
        if groups != expected_groups:
            raise RuntimeError(f"{target_db}.{table} group distribution mismatch: expected={expected_groups} actual={groups}")
        results.append(
            {
                "table": table,
                "row_count": digest.row_count,
                "canonical_sha256": digest.sha256,
                "groups": groups,
            }
        )
    return results


def ensure_direct_import_target_absent(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    tables: tuple[str, ...],
) -> None:
    existing = [table for table in tables if table_exists(conn, target_db, table)]
    if existing:
        raise RuntimeError(f"direct import target already exists in {target_db}: {existing}")


def ensure_schema_exists(conn: pymysql.connections.Connection, db_name: str, *, create: bool) -> None:
    validate_schema_name("target_db", db_name)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS schema_count FROM information_schema.schemata WHERE schema_name=%s", (db_name,))
        exists = int(cur.fetchone()["schema_count"]) > 0
        if exists:
            return
        if not create:
            raise RuntimeError(f"target schema does not exist: {db_name}")
        cur.execute(f"CREATE DATABASE {quote_id(db_name)} DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")


def drop_schema_if_unprotected(conn: pymysql.connections.Connection, db_name: str) -> None:
    validate_schema_name("drop_db", db_name)
    if db_name in PROTECTED_TARGETS:
        raise RuntimeError(f"refusing to drop protected schema: {db_name}")
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {quote_id(db_name)}")


def restore_dump_into_schema(*, target_db: str, dump_path: Path) -> RestoreResult:
    client = shutil.which("mariadb") or shutil.which("mysql")
    if not client:
        raise RuntimeError("mariadb/mysql client not found in PATH")
    if not dump_path.exists():
        raise FileNotFoundError(f"dump path not found: {dump_path}")
    db_env = _db_env()
    password = db_env.get("MARIADB_ROOT_PASSWORD") or db_env.get("MARIADB_PASSWORD")
    user = "root" if db_env.get("MARIADB_ROOT_PASSWORD") else db_env.get("MARIADB_USER", "jwapp")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    command = [
        client,
        f"--host={db_env.get('MARIADB_HOST', '127.0.0.1')}",
        f"--port={db_env.get('MARIADB_PORT') or db_env.get('HOST_PORT', '3307')}",
        f"--user={user}",
        target_db,
    ]
    start = time.perf_counter()
    if dump_path.suffix == ".gz":
        _restore_gzip_dump(command, env, dump_path)
    else:
        _restore_plain_dump(command, env, dump_path)
    return RestoreResult(path=dump_path, size_bytes=dump_path.stat().st_size, elapsed_seconds=time.perf_counter() - start)


def _restore_plain_dump(command: list[str], env: Mapping[str, str], dump_path: Path) -> None:
    with dump_path.open("rb") as dump_file:
        _restore_dump_stream(command, env, dump_file)


def _restore_gzip_dump(command: list[str], env: Mapping[str, str], dump_path: Path) -> None:
    with gzip.open(dump_path, "rb") as dump_file:
        _restore_dump_stream(command, env, dump_file)


def _restore_dump_stream(command: list[str], env: Mapping[str, str], dump_file: BinaryIO) -> None:
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, env=env)
    try:
        if proc.stdin is None:
            raise RuntimeError("restore command did not expose stdin")
        _copy_writeset_safe_restore_stream(dump_file, proc.stdin)
        proc.stdin.close()
        return_code = proc.wait()
    except BrokenPipeError as exc:
        return_code = proc.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command) from exc
        raise
    except Exception:
        proc.kill()
        proc.wait()
        raise
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def _copy_writeset_safe_restore_stream(dump_file: BinaryIO, target: BinaryIO) -> None:
    for line in dump_file:
        if _is_restore_transaction_wrapper(line):
            continue
        target.write(line)


def _is_restore_transaction_wrapper(line: bytes) -> bool:
    stripped = line.strip()
    if stripped in RESTORE_TRANSACTION_WRAPPER_LINES:
        return True
    return any(stripped.startswith(prefix) for prefix in RESTORE_TRANSACTION_WRAPPER_PREFIXES)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entries(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = manifest.get("tables")
    if not isinstance(entries, list):
        raise RuntimeError("manifest is missing table entries")
    return entries


def _render_groups(groups: Mapping[tuple[str, ...], int]) -> dict[str, int]:
    return {"|".join(key): value for key, value in groups.items()}
