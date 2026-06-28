from __future__ import annotations

"""Tracked dynamic mart/sidecar importer for the test2 serving schema.

STAGE A needs to load the same dynamic table set that was verified locally into
the test2 backend schema.  The older builders deliberately allow only isolated
schemas or localhost ``jw_mart`` serving writes, so using raw shell snippets to
patch test2 would be unreproducible.  This CLI is the narrow tracked path for
that gap: dump the approved dynamic table set from a verified local source,
restore it only into an explicitly allowed test2 serving target, and verify the
row counts from the manifest.

The guard is intentionally asymmetric.  Local ``jw_mart`` may be a source of
verified mart/sidecar rows, but ``jw_mart`` is never an allowed target here.
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping


DYNAMIC_SERVING_TABLES: Final[tuple[str, ...]] = (
    "mart_general_brand_metric",
    "mart_general_market_metric",
    "mart_general_filter_dimension_metric",
    "mart_strategic_filter_dimension_metric",
    "mart_strategic_ml_brand_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_market_metric",
)
CACHE_TABLES: Final[tuple[str, ...]] = (
    "cache_brands",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
)
LOCAL_SOURCE_SCHEMA: Final[str] = "jw_mart"
TEST2_SERVING_SCHEMA: Final[str] = "jw_mart_d1_stage_20260625_173115"
TEST2_HOST_FRAGMENT: Final[str] = "llmops-mariadb-service"
GENERAL_FILTER_TABLE: Final[str] = "mart_general_filter_dimension_metric"
GENERAL_BRAND_TABLE: Final[str] = "mart_general_brand_metric"
GENERAL_OPTION_INDEXES: Final[tuple[tuple[str, str], ...]] = (
    (
        "idx_general_option_universe",
        "(`source`, `dimension_type`, `dimension_value_hash`, `dimension_value_norm`(191))",
    ),
    (
        "idx_general_atc_scope",
        "(`source`, `atc4_code`, `dimension_type`, `dimension_value_hash`)",
    ),
    (
        "idx_general_brand_scope",
        "(`source`, `atc4_code`, `brand_key`, `dimension_type`, `dimension_value_hash`)",
    ),
)
GENERAL_BRAND_INDEXES: Final[tuple[tuple[str, str], ...]] = (
    (
        "idx_general_atc_universe",
        "(`source`, `atc4_code`, `atc4_desc`)",
    ),
)
BLOCKED_TARGET_SCHEMAS: Final[frozenset[str]] = frozenset(
    {
        "jw_mart",
        "mysql",
        "information_schema",
        "performance_schema",
        "sys",
    }
)


@dataclass(frozen=True, slots=True)
class DbEndpoint:
    host: str
    port: str
    user: str
    password: str


@dataclass(frozen=True, slots=True)
class TableCount:
    table: str
    rows: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=LOCAL_SOURCE_SCHEMA)
    parser.add_argument("--target-db")
    parser.add_argument("--run-id")
    parser.add_argument("--dump-path", type=Path)
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--output-manifest-path", type=Path)
    parser.add_argument("--source-env-file", type=Path)
    parser.add_argument("--target-env-file", type=Path)
    parser.add_argument("--backup-target-dump", type=Path)
    parser.add_argument("--dump-only", action="store_true")
    parser.add_argument("--import-from-dump", action="store_true")
    parser.add_argument(
        "--include-cache",
        action="store_true",
        help=(
            "Also move cache_* tables. Dynamic API 200 only needs mart/sidecar "
            "tables, so cache is opt-in to avoid rewriting unrelated test2 cache."
        ),
    )
    parser.add_argument(
        "--allow-test2-serving-target",
        action="store_true",
        help=(
            "Permit restoring into the known test2 serving schema. This does not "
            "permit live jw_mart or arbitrary Galera schemas."
        ),
    )
    parser.add_argument(
        "--target-via-port-forward",
        action="store_true",
        help=(
            "Allow target host 127.0.0.1/localhost only for an operator-managed "
            "kubectl port-forward to the test2 MariaDB service."
        ),
    )
    parser.add_argument(
        "--apply-general-option-indexes",
        action="store_true",
        help=(
            "Create the tracked general sidecar indexes needed by test2 "
            "filter-options. Guarded to the known test2 serving schema only."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode_count = sum(bool(value) for value in (args.dump_only, args.import_from_dump, args.apply_general_option_indexes))
    if mode_count != 1:
        raise SystemExit("choose exactly one of --dump-only, --import-from-dump, or --apply-general-option-indexes")
    if args.dump_only:
        if not args.run_id or not args.dump_path or not args.manifest_path:
            raise SystemExit("--run-id, --dump-path, and --manifest-path are required with --dump-only")
        manifest = dump_verified_source(
            source_db=str(args.source_db),
            run_id=str(args.run_id),
            dump_path=args.dump_path,
            manifest_path=args.manifest_path,
            env_file=args.source_env_file,
            include_cache=bool(args.include_cache),
        )
        print(json.dumps({"mode": "dump_only", "manifest": str(args.manifest_path), "rows": manifest["tables"]}, ensure_ascii=False))
        return 0
    if args.apply_general_option_indexes:
        if not args.target_db:
            raise SystemExit("--target-db is required with --apply-general-option-indexes")
        manifest = apply_general_option_indexes(
            target_db=str(args.target_db),
            env_file=args.target_env_file,
            output_manifest_path=args.output_manifest_path,
            allow_test2_serving_target=bool(args.allow_test2_serving_target),
            target_via_port_forward=bool(args.target_via_port_forward),
        )
        print(json.dumps({"mode": "apply_general_option_indexes", "indexes": manifest["indexes"]}, ensure_ascii=False))
        return 0
    if not args.target_db:
        raise SystemExit("--target-db is required with --import-from-dump")
    if not args.dump_path or not args.manifest_path:
        raise SystemExit("--dump-path and --manifest-path are required with --import-from-dump")
    manifest = import_test2_serving(
        target_db=str(args.target_db),
        dump_path=args.dump_path,
        manifest_path=args.manifest_path,
        output_manifest_path=args.output_manifest_path or args.manifest_path,
        env_file=args.target_env_file,
        backup_target_dump=args.backup_target_dump,
        allow_test2_serving_target=bool(args.allow_test2_serving_target),
        target_via_port_forward=bool(args.target_via_port_forward),
        include_cache=bool(args.include_cache),
    )
    print(json.dumps({"mode": "import_from_dump", "manifest": str(args.output_manifest_path or args.manifest_path), "rows": manifest["verification"]}, ensure_ascii=False))
    return 0


def apply_general_option_indexes(
    *,
    target_db: str,
    env_file: Path | None,
    output_manifest_path: Path | None,
    allow_test2_serving_target: bool,
    target_via_port_forward: bool,
) -> dict[str, object]:
    """Apply only the test2 general option lookup indexes.

    This is intentionally part of the tracked importer instead of an operator
    SQL note.  It reuses the same target guard as table imports, so the live
    ``jw_mart`` schema and arbitrary Galera schemas remain blocked.
    """

    endpoint = _endpoint_from_env(env_file)
    _guard_target_endpoint(
        target_db,
        endpoint,
        allow_test2_serving_target=allow_test2_serving_target,
        target_via_port_forward=target_via_port_forward,
    )
    started = time.perf_counter()
    index_results: list[dict[str, object]] = []
    for table, indexes in ((GENERAL_FILTER_TABLE, GENERAL_OPTION_INDEXES), (GENERAL_BRAND_TABLE, GENERAL_BRAND_INDEXES)):
        for index_name, definition in indexes:
            if _index_exists(endpoint, target_db, table, index_name):
                index_results.append({"table": table, "index": index_name, "status": "exists"})
                continue
            _mysql_execute(
                endpoint,
                f"ALTER TABLE `{target_db}`.`{table}` ADD INDEX `{index_name}` {definition}",
            )
            index_results.append({"table": table, "index": index_name, "status": "created"})
    manifest: dict[str, object] = {
        "mode": "apply_general_option_indexes",
        "target_db": target_db,
        "tables": [GENERAL_FILTER_TABLE, GENERAL_BRAND_TABLE],
        "indexes": index_results,
        "policy": {
            "allow_test2_serving_target": allow_test2_serving_target,
            "target_via_port_forward": target_via_port_forward,
            "target_jw_mart_blocked": True,
            "test2_host_fragment": TEST2_HOST_FRAGMENT,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    if output_manifest_path:
        _write_json(output_manifest_path, manifest)
    return manifest


def dump_verified_source(
    *,
    source_db: str,
    run_id: str,
    dump_path: Path,
    manifest_path: Path,
    env_file: Path | None,
    include_cache: bool,
) -> dict[str, object]:
    endpoint = _endpoint_from_env(env_file)
    _guard_source_endpoint(source_db, endpoint)
    tables = _selected_tables(include_cache=include_cache)
    started = time.perf_counter()
    counts = _fetch_counts(endpoint, source_db, tables)
    _dump_tables(endpoint, source_db, tables, dump_path)
    manifest: dict[str, object] = {
        "run_id": run_id,
        "mode": "dump_only",
        "source_db": source_db,
        "tables": [_table_count_payload(count) for count in counts],
        "dump": {
            "path": str(dump_path),
            "size_bytes": dump_path.stat().st_size,
            "sha256": _sha256_file(dump_path),
        },
        "policy": {
            "tables": list(tables),
            "cache_included": include_cache,
            "local_jw_mart_source_only": True,
            "target_jw_mart_blocked": True,
            "test2_serving_schema": TEST2_SERVING_SCHEMA,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    _write_json(manifest_path, manifest)
    return manifest


def import_test2_serving(
    *,
    target_db: str,
    dump_path: Path,
    manifest_path: Path,
    output_manifest_path: Path,
    env_file: Path | None,
    backup_target_dump: Path | None,
    allow_test2_serving_target: bool,
    target_via_port_forward: bool,
    include_cache: bool,
) -> dict[str, object]:
    manifest = _read_json(manifest_path)
    endpoint = _endpoint_from_env(env_file)
    _guard_target_endpoint(
        target_db,
        endpoint,
        allow_test2_serving_target=allow_test2_serving_target,
        target_via_port_forward=target_via_port_forward,
    )
    if backup_target_dump:
        existing = _existing_tables(endpoint, target_db, _selected_tables(include_cache=include_cache))
        if existing:
            _dump_tables(endpoint, target_db, existing, backup_target_dump)
    started = time.perf_counter()
    _restore_dump(endpoint, target_db, dump_path)
    verification = _verify_counts(endpoint, target_db, manifest)
    imported: dict[str, object] = {
        "run_id": str(manifest.get("run_id") or ""),
        "mode": "import_from_dump",
        "target_db": target_db,
        "verification": [_table_count_payload(count) for count in verification],
        "backup_target_dump": str(backup_target_dump) if backup_target_dump else None,
        "policy": {
            "allow_test2_serving_target": allow_test2_serving_target,
            "target_via_port_forward": target_via_port_forward,
            "cache_included": include_cache,
            "target_jw_mart_blocked": True,
            "test2_host_fragment": TEST2_HOST_FRAGMENT,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    _write_json(output_manifest_path, imported)
    return imported


def _endpoint_from_env(env_file: Path | None) -> DbEndpoint:
    merged = dict(os.environ)
    if env_file:
        merged.update(_read_env_file(env_file))
    password = merged.get("MARIADB_ROOT_PASSWORD") or merged.get("MARIADB_PASSWORD") or merged.get("DB_PASSWORD")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD/DB_PASSWORD is missing")
    user = "root" if merged.get("MARIADB_ROOT_PASSWORD") else merged.get("MARIADB_USER") or merged.get("DB_USER") or "jwapp"
    return DbEndpoint(
        host=merged.get("MARIADB_HOST") or merged.get("DB_HOST") or "127.0.0.1",
        port=merged.get("MARIADB_PORT") or merged.get("HOST_PORT") or merged.get("DB_PORT") or "3308",
        user=user,
        password=password,
    )


def _selected_tables(*, include_cache: bool) -> tuple[str, ...]:
    if include_cache:
        return (*DYNAMIC_SERVING_TABLES, *CACHE_TABLES)
    return DYNAMIC_SERVING_TABLES


def _read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _guard_source_endpoint(source_db: str, endpoint: DbEndpoint) -> None:
    if source_db != LOCAL_SOURCE_SCHEMA:
        raise RuntimeError(f"dynamic serving source must be {LOCAL_SOURCE_SCHEMA}: {source_db}")
    if endpoint.host.strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"refusing non-local source host for {LOCAL_SOURCE_SCHEMA}: {endpoint.host}")
    if endpoint.port not in {"3306", "3308"}:
        raise RuntimeError(f"refusing unexpected local source port: {endpoint.port}")


def _guard_target_endpoint(
    target_db: str,
    endpoint: DbEndpoint,
    *,
    allow_test2_serving_target: bool,
    target_via_port_forward: bool = False,
) -> None:
    if target_db in BLOCKED_TARGET_SCHEMAS:
        raise RuntimeError(f"refusing protected target schema: {target_db}")
    if not allow_test2_serving_target:
        raise RuntimeError("--allow-test2-serving-target is required for serving schema imports")
    if target_db != TEST2_SERVING_SCHEMA:
        raise RuntimeError(f"test2 serving import only permits {TEST2_SERVING_SCHEMA}: {target_db}")
    if target_via_port_forward and endpoint.host.strip().lower() in {"127.0.0.1", "localhost", "::1"}:
        return
    if TEST2_HOST_FRAGMENT not in endpoint.host:
        raise RuntimeError(f"refusing target host without {TEST2_HOST_FRAGMENT!r}: {endpoint.host}")


def _fetch_counts(endpoint: DbEndpoint, db_name: str, tables: tuple[str, ...]) -> tuple[TableCount, ...]:
    counts: list[TableCount] = []
    for table in tables:
        rows = _mysql_scalar(endpoint, f"SELECT COUNT(*) FROM `{db_name}`.`{table}`")
        counts.append(TableCount(table=table, rows=int(rows)))
    return tuple(counts)


def _verify_counts(endpoint: DbEndpoint, target_db: str, manifest: Mapping[str, object]) -> tuple[TableCount, ...]:
    expected = {str(row["table"]): int(row["rows"]) for row in _manifest_tables(manifest)}
    actual = _fetch_counts(endpoint, target_db, tuple(expected))
    mismatches = [f"{count.table}: expected={expected[count.table]} actual={count.rows}" for count in actual if expected[count.table] != count.rows]
    if mismatches:
        raise RuntimeError("row count verification failed: " + "; ".join(mismatches))
    return actual


def _existing_tables(endpoint: DbEndpoint, db_name: str, tables: tuple[str, ...]) -> tuple[str, ...]:
    existing: list[str] = []
    for table in tables:
        rows = _mysql_scalar(
            endpoint,
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema='{_sql_literal(db_name)}' AND table_name='{_sql_literal(table)}'",
        )
        if int(rows) > 0:
            existing.append(table)
    return tuple(existing)


def _dump_tables(endpoint: DbEndpoint, db_name: str, tables: tuple[str, ...], dump_path: Path) -> None:
    dump_bin = shutil.which("mariadb-dump") or shutil.which("mysqldump")
    if not dump_bin:
        raise RuntimeError("mariadb-dump/mysqldump not found in PATH")
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        dump_bin,
        f"--host={endpoint.host}",
        f"--port={endpoint.port}",
        f"--user={endpoint.user}",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--skip-add-locks",
        "--skip-extended-insert",
        "--skip-disable-keys",
        "--skip-no-autocommit",
        db_name,
        *tables,
    ]
    env = _client_env(endpoint)
    if dump_path.suffix == ".gz":
        with subprocess.Popen(command, stdout=subprocess.PIPE, env=env) as proc:
            if proc.stdout is None:
                raise RuntimeError("dump command did not expose stdout")
            with gzip.open(dump_path, "wb") as out:
                shutil.copyfileobj(proc.stdout, out, length=1024 * 1024)
            return_code = proc.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
        return
    with dump_path.open("wb") as out:
        subprocess.run(command, check=True, stdout=out, env=env)


def _restore_dump(endpoint: DbEndpoint, target_db: str, dump_path: Path) -> None:
    client = shutil.which("mariadb") or shutil.which("mysql")
    if not client:
        raise RuntimeError("mariadb/mysql client not found in PATH")
    command = [client, f"--host={endpoint.host}", f"--port={endpoint.port}", f"--user={endpoint.user}", target_db]
    env = _client_env(endpoint)
    if dump_path.suffix == ".gz":
        # Do not pass a GzipFile directly as subprocess stdin: subprocess uses
        # the wrapped file descriptor and MariaDB would receive compressed bytes.
        with gzip.open(dump_path, "rb") as handle, subprocess.Popen(command, stdin=subprocess.PIPE, env=env) as proc:
            if proc.stdin is None:
                raise RuntimeError("restore command did not expose stdin")
            try:
                shutil.copyfileobj(handle, proc.stdin, length=1024 * 1024)
            finally:
                proc.stdin.close()
            return_code = proc.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
        return
    with dump_path.open("rb") as handle:
        subprocess.run(command, check=True, stdin=handle, env=env)


def _mysql_scalar(endpoint: DbEndpoint, sql: str) -> str:
    client = shutil.which("mariadb") or shutil.which("mysql")
    if not client:
        raise RuntimeError("mariadb/mysql client not found in PATH")
    command = [
        client,
        f"--host={endpoint.host}",
        f"--port={endpoint.port}",
        f"--user={endpoint.user}",
        "--batch",
        "--skip-column-names",
        "--execute",
        sql,
    ]
    output = subprocess.check_output(command, env=_client_env(endpoint), text=True).strip()
    if "\n" in output:
        raise RuntimeError(f"expected scalar output for SQL, got: {output!r}")
    return output


def _mysql_execute(endpoint: DbEndpoint, sql: str) -> None:
    client = shutil.which("mariadb") or shutil.which("mysql")
    if client:
        command = [
            client,
            f"--host={endpoint.host}",
            f"--port={endpoint.port}",
            f"--user={endpoint.user}",
            "--execute",
            sql,
        ]
        subprocess.run(command, check=True, env=_client_env(endpoint))
        return
    _pymysql_execute(endpoint, sql)


def _index_exists(endpoint: DbEndpoint, db_name: str, table: str, index_name: str) -> bool:
    sql = (
        "SELECT COUNT(*) FROM information_schema.statistics "
        f"WHERE table_schema='{_sql_literal(db_name)}' "
        f"AND table_name='{_sql_literal(table)}' "
        f"AND index_name='{_sql_literal(index_name)}'"
    )
    try:
        return int(_mysql_scalar(endpoint, sql)) > 0
    except RuntimeError as exc:
        if "client not found" not in str(exc):
            raise
    return int(_pymysql_scalar(endpoint, sql)) > 0


def _pymysql_scalar(endpoint: DbEndpoint, sql: str) -> str:
    import pymysql

    with pymysql.connect(
        host=endpoint.host,
        port=int(endpoint.port),
        user=endpoint.user,
        password=endpoint.password,
        charset="utf8mb4",
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    if not row:
        return ""
    return str(row[0])


def _pymysql_execute(endpoint: DbEndpoint, sql: str) -> None:
    import pymysql

    with pymysql.connect(
        host=endpoint.host,
        port=int(endpoint.port),
        user=endpoint.user,
        password=endpoint.password,
        charset="utf8mb4",
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def _client_env(endpoint: DbEndpoint) -> dict[str, str]:
    env = os.environ.copy()
    env["MYSQL_PWD"] = endpoint.password
    return env


def _manifest_tables(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    tables = manifest.get("tables")
    if not isinstance(tables, list):
        raise RuntimeError("manifest is missing tables")
    return tables


def _table_count_payload(count: TableCount) -> dict[str, object]:
    return {"table": count.table, "rows": count.rows}


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


if __name__ == "__main__":
    raise SystemExit(main())
