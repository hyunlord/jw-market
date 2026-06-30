from __future__ import annotations

"""Tracked STAGE A importer for the strategic/cache 8-table reload set.

This script exists because the first operating Galera staging load was done
through a one-off loader. Future staging writes must be reproducible from a
tracked CLI: dump the exact eight verified tables, restore them only into a new
`jw_mart_d1_stage_*` schema, and persist a manifest that can be replayed or
used to verify an existing staging schema. It deliberately does not publish,
promote, or touch live `jw_mart`.
"""

import argparse
import gzip
import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

import pymysql

from pipeline.scripts.deploy.mart_import_ops import RestoreResult
from pipeline.scripts.deploy.mart_import_ops import _is_restore_transaction_wrapper
from pipeline.scripts.deploy.mart_import_ops import attach_dump_to_manifest
from pipeline.scripts.deploy.mart_import_ops import capture_manifest
from pipeline.scripts.deploy.mart_import_ops import ensure_direct_import_target_absent
from pipeline.scripts.deploy.mart_import_ops import ensure_schema_exists
from pipeline.scripts.deploy.mart_import_ops import drop_schema_if_unprotected
from pipeline.scripts.deploy.mart_import_ops import restore_dump_into_schema
from pipeline.scripts.deploy.mart_import_ops import sha256_file
from pipeline.scripts.deploy.mart_import_ops import verify_against_manifest
from pipeline.scripts.deploy.mart_load_ops import DumpResult
from pipeline.scripts.deploy.mart_load_ops import connect_admin
from pipeline.scripts.deploy.mart_load_ops import db_endpoint_summary
from pipeline.scripts.deploy.mart_load_ops import dump_tables
from pipeline.scripts.deploy.mart_load_ops import load_env_file
from pipeline.scripts.deploy.mart_load_ops import validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import quote_id
from pipeline.scripts.deploy.strategic_reload_publish import STRATEGIC_RELOAD_TABLES
from pipeline.scripts.deploy.strategic_reload_publish import validate_publish_tables


STAGE_IMPORT_TABLES = STRATEGIC_RELOAD_TABLES
STAGE_SCHEMA_PREFIX = "jw_mart_d1_stage_"
BLOCKED_TARGET_SCHEMAS = frozenset(
    {
        "jw_mart",
        "jw_mart_test_stage2",
        "jw_mart_d1_stage_20260625_173115",
    }
)


@dataclass(frozen=True, slots=True)
class DumpOnlyConfig:
    source_db: str
    run_id: str
    dump_path: Path
    manifest_path: Path
    source_env_file: Path | None = None


@dataclass(frozen=True, slots=True)
class ManifestOnlyConfig:
    source_db: str
    run_id: str
    dump_path: Path
    manifest_path: Path
    source_env_file: Path | None = None


@dataclass(frozen=True, slots=True)
class ImportConfig:
    target_db: str
    dump_path: Path
    manifest_path: Path
    output_manifest_path: Path
    target_env_file: Path | None = None
    restore_shell_command: str | None = None
    raw_restore_stream: bool = False
    drop_target_after_success: bool = False


@dataclass(frozen=True, slots=True)
class DumpImportConfig:
    source_db: str
    target_db: str
    run_id: str
    dump_path: Path
    manifest_path: Path
    source_env_file: Path | None = None
    target_env_file: Path | None = None
    drop_target_after_success: bool = False


@dataclass(frozen=True, slots=True)
class CompareConfig:
    target_db: str
    manifest_path: Path
    output_manifest_path: Path
    target_env_file: Path | None = None


def guard_stage_import(*, source_db: str, target_db: str) -> None:
    validate_schema_name("source_db", source_db)
    validate_schema_name("target_db", target_db)
    if source_db == target_db:
        raise ValueError(f"source_db and target_db must differ: {target_db}")
    if target_db in BLOCKED_TARGET_SCHEMAS:
        raise ValueError(f"refusing protected or already-validated target schema: {target_db}")
    if not target_db.startswith(STAGE_SCHEMA_PREFIX):
        raise ValueError(f"target_db must be a new staging schema with prefix {STAGE_SCHEMA_PREFIX}: {target_db}")
    validate_publish_tables(STAGE_IMPORT_TABLES)


def run_dump_only(config: DumpOnlyConfig) -> dict[str, object]:
    validate_schema_name("source_db", config.source_db)
    with _temporary_env_file(config.source_env_file):
        started = time.perf_counter()
        dump_result = dump_tables(target_db=config.source_db, tables=STAGE_IMPORT_TABLES, dump_path=config.dump_path)
        conn = connect_admin()
        try:
            manifest = capture_manifest(
                conn,
                run_id=config.run_id,
                source_db=config.source_db,
                build_db=config.source_db,
                tables=STAGE_IMPORT_TABLES,
            )
        finally:
            conn.close()
        manifest = attach_dump_to_manifest(manifest, dump_path=dump_result.path, dump_seconds=dump_result.elapsed_seconds)
        _attach_policy(manifest, mode="dump_only")
        manifest["source_endpoint"] = db_endpoint_summary()
        manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        _write_json(config.manifest_path, manifest)
        return {"mode": "dump_only", "dump": manifest["dump"], "manifest_path": str(config.manifest_path)}


def run_manifest_only(config: ManifestOnlyConfig) -> dict[str, object]:
    validate_schema_name("source_db", config.source_db)
    if not config.dump_path.exists():
        raise FileNotFoundError(f"dump path not found: {config.dump_path}")
    with _temporary_env_file(config.source_env_file):
        conn = connect_admin()
        try:
            manifest = capture_manifest(
                conn,
                run_id=config.run_id,
                source_db=config.source_db,
                build_db=config.source_db,
                tables=STAGE_IMPORT_TABLES,
            )
        finally:
            conn.close()
        manifest = attach_dump_to_manifest(manifest, dump_path=config.dump_path, dump_seconds=0.0)
        _attach_policy(manifest, mode="manifest_only")
        manifest["source_endpoint"] = db_endpoint_summary()
        _write_json(config.manifest_path, manifest)
        return {"mode": "manifest_only", "dump": manifest["dump"], "manifest_path": str(config.manifest_path)}


def run_import_from_dump(config: ImportConfig) -> dict[str, object]:
    guard_stage_import(source_db="manifest_source", target_db=config.target_db)
    manifest = _read_manifest(config.manifest_path)
    with _temporary_env_file(config.target_env_file):
        conn = connect_admin()
        try:
            if schema_exists(conn, config.target_db):
                raise RuntimeError(f"target staging schema already exists: {config.target_db}")
            ensure_schema_exists(conn, config.target_db, create=True)
            ensure_direct_import_target_absent(conn, target_db=config.target_db, tables=STAGE_IMPORT_TABLES)
            if config.restore_shell_command:
                if config.raw_restore_stream:
                    restore_result = restore_raw_dump_with_shell_command(
                        target_db=config.target_db,
                        dump_path=config.dump_path,
                        shell_command=config.restore_shell_command,
                    )
                else:
                    restore_result = restore_dump_with_shell_command(
                        target_db=config.target_db,
                        dump_path=config.dump_path,
                        shell_command=config.restore_shell_command,
                    )
            else:
                restore_result = restore_dump_into_schema(target_db=config.target_db, dump_path=config.dump_path)
            verification = verify_against_manifest(conn, target_db=config.target_db, manifest=manifest)
            imported = _import_manifest_payload(
                source_manifest=manifest,
                target_db=config.target_db,
                target_endpoint=db_endpoint_summary(),
                restore_result=restore_result,
                verification=verification,
            )
            if config.drop_target_after_success:
                drop_schema_if_unprotected(conn, config.target_db)
                imported["cleanup"] = {"target_db_dropped": True}
            else:
                imported["cleanup"] = {"target_db_dropped": False}
            _write_json(config.output_manifest_path, imported)
        finally:
            conn.close()
    return {
        "mode": "import_from_dump",
        "target_db": config.target_db,
        "verification": verification,
        "manifest_path": str(config.output_manifest_path),
        "cleanup": imported["cleanup"],
    }


def run_compare_schema(config: CompareConfig) -> dict[str, object]:
    validate_schema_name("target_db", config.target_db)
    manifest = _read_manifest(config.manifest_path)
    with _temporary_env_file(config.target_env_file):
        conn = connect_admin()
        try:
            if not schema_exists(conn, config.target_db):
                raise RuntimeError(f"comparison schema does not exist: {config.target_db}")
            verification = verify_against_manifest(conn, target_db=config.target_db, manifest=manifest)
            payload = {
                "mode": "compare_schema",
                "target_db": config.target_db,
                "target_endpoint": db_endpoint_summary(),
                "verification": verification,
                "source_manifest": str(config.manifest_path),
            }
            _write_json(config.output_manifest_path, payload)
        finally:
            conn.close()
    return {"mode": "compare_schema", "target_db": config.target_db, "verification": verification}


def run_dump_import(config: DumpImportConfig) -> dict[str, object]:
    guard_stage_import(source_db=config.source_db, target_db=config.target_db)
    dump_summary = run_dump_only(
        DumpOnlyConfig(
            source_db=config.source_db,
            run_id=config.run_id,
            dump_path=config.dump_path,
            manifest_path=config.manifest_path,
            source_env_file=config.source_env_file,
        )
    )
    import_summary = run_import_from_dump(
        ImportConfig(
            target_db=config.target_db,
            dump_path=config.dump_path,
            manifest_path=config.manifest_path,
            output_manifest_path=_default_import_manifest_path(config.manifest_path),
            target_env_file=config.target_env_file,
            drop_target_after_success=config.drop_target_after_success,
        )
    )
    return {"mode": "dump_import", "dump": dump_summary, "import": import_summary}


def _default_import_manifest_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.stem}.imported{manifest_path.suffix}")


def schema_exists(conn: pymysql.connections.Connection, db_name: str) -> bool:
    validate_schema_name("target_db", db_name)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS schema_count FROM information_schema.schemata WHERE schema_name=%s", (db_name,))
        row = cur.fetchone()
    return int(row["schema_count"]) > 0


def stage_row_counts(conn: pymysql.connections.Connection, db_name: str) -> dict[str, int]:
    return {table: _table_row_count(conn, db_name, table) for table in STAGE_IMPORT_TABLES}


def restore_dump_with_shell_command(*, target_db: str, dump_path: Path, shell_command: str) -> RestoreResult:
    """Stream a writeset-safe dump into an explicit restore command.

    This is for locked-down operating networks where MySQL is only reachable
    through an approved transport such as `ssh ... kubectl exec -i ...`. The
    command is not allowed to choose tables or targets; schema creation and
    manifest verification still happen in this tracked script.
    """
    validate_schema_name("target_db", target_db)
    if not dump_path.exists():
        raise FileNotFoundError(f"dump path not found: {dump_path}")
    started = time.perf_counter()
    proc = subprocess.Popen(shell_command, shell=True, stdin=subprocess.PIPE, env=os.environ.copy())
    try:
        if proc.stdin is None:
            raise RuntimeError("restore shell command did not expose stdin")
        if dump_path.suffix == ".gz":
            with gzip.open(dump_path, "rb") as dump_file:
                _copy_restore_stream_with_insert_batches(dump_file, proc.stdin)
        else:
            with dump_path.open("rb") as dump_file:
                _copy_restore_stream_with_insert_batches(dump_file, proc.stdin)
        proc.stdin.close()
        return_code = proc.wait()
    except BrokenPipeError as exc:
        return_code = proc.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, shell_command) from exc
        raise
    except Exception:
        proc.kill()
        proc.wait()
        raise
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, shell_command)
    return RestoreResult(path=dump_path, size_bytes=dump_path.stat().st_size, elapsed_seconds=time.perf_counter() - started)


def restore_raw_dump_with_shell_command(*, target_db: str, dump_path: Path, shell_command: str) -> RestoreResult:
    """Stream the dump file bytes unchanged to a restore command.

    Use this when the receiving command handles decompression remotely, for
    example `gzip -dc | mariadb`. The dump must already be Galera-safe
    (`--skip-extended-insert`) because this path does not rewrite SQL.
    """
    validate_schema_name("target_db", target_db)
    if not dump_path.exists():
        raise FileNotFoundError(f"dump path not found: {dump_path}")
    started = time.perf_counter()
    proc = subprocess.Popen(shell_command, shell=True, stdin=subprocess.PIPE, env=os.environ.copy())
    try:
        if proc.stdin is None:
            raise RuntimeError("restore shell command did not expose stdin")
        with dump_path.open("rb") as dump_file:
            for chunk in iter(lambda: dump_file.read(1024 * 1024), b""):
                proc.stdin.write(chunk)
        proc.stdin.close()
        return_code = proc.wait()
    except BrokenPipeError as exc:
        return_code = proc.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, shell_command) from exc
        raise
    except Exception:
        proc.kill()
        proc.wait()
        raise
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, shell_command)
    return RestoreResult(path=dump_path, size_bytes=dump_path.stat().st_size, elapsed_seconds=time.perf_counter() - started)


def _copy_restore_stream_with_insert_batches(dump_file: object, target: object, *, insert_batch_size: int = 200) -> None:
    """Copy dump SQL while grouping row INSERTs into bounded transactions."""
    open_batch = 0
    for line in dump_file:
        if _is_restore_transaction_wrapper(line):
            continue
        if _is_insert_line(line):
            if open_batch == 0:
                target.write(b"START TRANSACTION;\n")
            target.write(line)
            open_batch += 1
            if open_batch >= insert_batch_size:
                target.write(b"COMMIT;\n")
                open_batch = 0
            continue
        if open_batch:
            target.write(b"COMMIT;\n")
            open_batch = 0
        target.write(line)
    if open_batch:
        target.write(b"COMMIT;\n")


def _is_insert_line(line: bytes) -> bool:
    return line.lstrip().upper().startswith(b"INSERT ")


def _table_row_count(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS row_count FROM {quote_id(db_name)}.{quote_id(table_name)}")
        row = cur.fetchone()
    return int(row["row_count"])


def _attach_policy(manifest: dict[str, object], *, mode: str) -> None:
    manifest["policy"] = {
        "mode": mode,
        "tables": list(STAGE_IMPORT_TABLES),
        "target_prefix": STAGE_SCHEMA_PREFIX,
        "blocked_target_schemas": sorted(BLOCKED_TARGET_SCHEMAS),
        "galera_notes": "dump uses --skip-extended-insert and restore strips dump transaction wrappers; no CTAS path",
    }


def _import_manifest_payload(
    *,
    source_manifest: Mapping[str, object],
    target_db: str,
    target_endpoint: Mapping[str, str],
    restore_result: RestoreResult,
    verification: list[dict[str, object]],
) -> dict[str, object]:
    payload = dict(source_manifest)
    _attach_policy(payload, mode="import_from_dump")
    payload["target_db"] = target_db
    payload["target_endpoint"] = dict(target_endpoint)
    payload["restore"] = {
        "path": str(restore_result.path),
        "size_bytes": restore_result.size_bytes,
        "sha256": sha256_file(restore_result.path),
        "elapsed_seconds": round(restore_result.elapsed_seconds, 3),
    }
    payload["verification"] = verification
    return payload


def _read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"manifest root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@contextmanager
def _temporary_env_file(path: Path | None) -> Iterator[None]:
    original = os.environ.copy()
    try:
        load_env_file(path)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reproduce the MI Master strategic/cache STAGE A path by dumping exactly the eight "
            "tracked reload tables from a verified source schema and restoring them into a new "
            "jw_mart_d1_stage_* Galera staging schema."
        )
    )
    parser.add_argument("--source-db", default="jw_mart")
    parser.add_argument("--target-db")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dump-path", type=Path, required=True)
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--output-manifest-path", type=Path)
    parser.add_argument("--source-env-file", type=Path)
    parser.add_argument("--target-env-file", type=Path)
    parser.add_argument(
        "--restore-shell-command",
        help="Advanced: stream the safe dump to this command's stdin after creating the new staging schema.",
    )
    parser.add_argument(
        "--raw-restore-stream",
        action="store_true",
        help="Send dump bytes unchanged to --restore-shell-command; intended for remote gzip -dc | mariadb.",
    )
    parser.add_argument("--dump-only", action="store_true")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--import-from-dump", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    parser.add_argument(
        "--drop-target-after-success",
        action="store_true",
        help="Drop the safe staging schema after manifest verification succeeds; failed imports are preserved.",
    )
    args = parser.parse_args(argv)
    selected_modes = sum(bool(value) for value in (args.dump_only, args.manifest_only, args.import_from_dump, args.compare_only))
    if selected_modes > 1:
        parser.error("--dump-only, --manifest-only, --import-from-dump, and --compare-only are mutually exclusive")
    if not (args.dump_only or args.manifest_only) and not args.target_db:
        parser.error("--target-db is required unless --dump-only or --manifest-only is used")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dump_only:
        summary = run_dump_only(
            DumpOnlyConfig(
                source_db=str(args.source_db),
                run_id=str(args.run_id),
                dump_path=args.dump_path,
                manifest_path=args.manifest_path,
                source_env_file=args.source_env_file,
            )
        )
    elif args.manifest_only:
        summary = run_manifest_only(
            ManifestOnlyConfig(
                source_db=str(args.source_db),
                run_id=str(args.run_id),
                dump_path=args.dump_path,
                manifest_path=args.manifest_path,
                source_env_file=args.source_env_file,
            )
        )
    elif args.import_from_dump:
        summary = run_import_from_dump(
            ImportConfig(
                target_db=str(args.target_db),
                dump_path=args.dump_path,
                manifest_path=args.manifest_path,
                output_manifest_path=args.output_manifest_path or args.manifest_path,
                target_env_file=args.target_env_file,
                restore_shell_command=args.restore_shell_command,
                raw_restore_stream=bool(args.raw_restore_stream),
                drop_target_after_success=bool(args.drop_target_after_success),
            )
        )
    elif args.compare_only:
        summary = run_compare_schema(
            CompareConfig(
                target_db=str(args.target_db),
                manifest_path=args.manifest_path,
                output_manifest_path=args.output_manifest_path or args.manifest_path,
                target_env_file=args.target_env_file,
            )
        )
    else:
        summary = run_dump_import(
            DumpImportConfig(
                source_db=str(args.source_db),
                target_db=str(args.target_db),
                run_id=str(args.run_id),
                dump_path=args.dump_path,
                manifest_path=args.manifest_path,
                source_env_file=args.source_env_file,
                target_env_file=args.target_env_file,
                drop_target_after_success=bool(args.drop_target_after_success),
            )
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
