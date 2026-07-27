"""Execute one full UBIST ingest against a pod-local disposable MariaDB."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable

import pymysql

from pipeline.scripts.ingest_hook import config, stage_log_runner
from pipeline.scripts.ingest_hook.test_run_census import build_change_census
from pipeline.scripts.ingest_hook.test_runs import TestRunStore


class IsolationContractError(RuntimeError):
    pass


_FORBIDDEN_ENV = (
    "INGEST_LOAD_TARGET_ROOT",
    "INGEST_MART_PROMOTION_APPROVED",
)


def _quote_identifier(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def _writable_column_names(rows: list[tuple[object, object]]) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in rows
        if "GENERATED" not in str(row[1] or "").upper()
    )


def validate_isolation_env(environ: dict[str, str]) -> None:
    for name in _FORBIDDEN_ENV:
        if environ.get(name):
            raise IsolationContractError(f"{name} must be absent in a test load")
    if environ.get("MARIADB_HOST") not in {"127.0.0.1", "localhost"}:
        raise IsolationContractError("test target MARIADB_HOST must be pod-local")
    if not environ.get("MARIADB_DATABASE", "").startswith("jw_mart_test_"):
        raise IsolationContractError("test target database must start with jw_mart_test_")
    if not environ.get("INGEST_SHADOW_TARGET_DB", "").startswith(
        "jw_mart_ingest_shadow_"
    ):
        raise IsolationContractError(
            "test publication database must start with jw_mart_ingest_shadow_"
        )
    if not environ.get("INGEST_SHADOW_BUILD_PREFIX", "").startswith(
        "jw_mart_ingest_shadow_"
    ):
        raise IsolationContractError(
            "test build database prefix must start with jw_mart_ingest_shadow_"
        )
    if not environ.get("MARIADB_PASSWORD") or (
        environ.get("MARIADB_ROOT_PASSWORD") != environ.get("MARIADB_PASSWORD")
    ):
        raise IsolationContractError(
            "disposable MariaDB passwords must be non-empty and identical"
        )
    source_host = environ.get("INGEST_TEST_SOURCE_DB_HOST", "").strip()
    if not source_host or source_host in {"127.0.0.1", "localhost"}:
        raise IsolationContractError("source DB must be a distinct read-only endpoint")
    for name in (
        "INGEST_TEST_SOURCE_DB_NAME",
        "INGEST_TEST_SOURCE_DB_USER",
        "INGEST_TEST_SOURCE_DB_PASSWORD",
        "INGEST_LEDGER_SQLITE",
        "INGEST_LOAD_SHADOW_ROOT",
        "INGEST_SHADOW_CATALOG_ROOT",
        "INGEST_TEST_SOURCE_CATALOG_ROOT",
    ):
        if not environ.get(name):
            raise IsolationContractError(f"{name} is required")


def _prepare_shadow_catalog(environ: dict[str, str]) -> dict[str, int]:
    source = Path(environ["INGEST_TEST_SOURCE_CATALOG_ROOT"]).resolve()
    target = Path(environ["INGEST_SHADOW_CATALOG_ROOT"]).resolve()
    shadow_root = Path(environ["INGEST_LOAD_SHADOW_ROOT"]).resolve()
    try:
        target.relative_to(shadow_root)
    except ValueError as exc:
        raise IsolationContractError(
            "test catalog target must be inside the disposable shadow root"
        ) from exc
    required = source / "strategic_brand" / "strategic_brand.parquet"
    if not required.is_file():
        raise RuntimeError(f"source catalog is missing: {required}")
    if target.exists():
        raise RuntimeError(f"disposable catalog target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return {"files": sum(1 for path in target.rglob("*") if path.is_file())}


def _wait_for_local_db(environ: dict[str, str], timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            connection = pymysql.connect(
                host=environ["MARIADB_HOST"],
                port=int(environ.get("MARIADB_PORT", "3306")),
                user=environ.get("MARIADB_USER", "root"),
                password=environ.get("MARIADB_PASSWORD", ""),
                connect_timeout=3,
            )
            connection.close()
            return
        except Exception as exc:  # MariaDB startup is an external boundary.
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"disposable MariaDB did not become ready: {last_error}")


def clone_operating_snapshot(environ: dict[str, str]) -> dict[str, int]:
    """Stream a transaction-consistent, read-only source snapshot into local DB."""
    _wait_for_local_db(environ)
    source = pymysql.connect(
        host=environ["INGEST_TEST_SOURCE_DB_HOST"],
        port=int(environ.get("INGEST_TEST_SOURCE_DB_PORT", "3306")),
        user=environ["INGEST_TEST_SOURCE_DB_USER"],
        password=environ["INGEST_TEST_SOURCE_DB_PASSWORD"],
        database=environ["INGEST_TEST_SOURCE_DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.SSCursor,
    )
    target = pymysql.connect(
        host=environ["MARIADB_HOST"],
        port=int(environ.get("MARIADB_PORT", "3306")),
        user=environ.get("MARIADB_USER", "root"),
        password=environ.get("MARIADB_PASSWORD", ""),
        charset="utf8mb4",
        autocommit=True,
    )
    target_db = environ["MARIADB_DATABASE"]
    table_count = 0
    row_count = 0
    try:
        with source.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
            tables = [str(row[0]) for row in cursor.fetchall()]
        with target.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}`")
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in tables:
            with source.cursor() as source_cursor:
                source_cursor.execute(f"SHOW CREATE TABLE `{table}`")
                create_sql = str(source_cursor.fetchone()[1])
                source_cursor.execute(
                    """
                    SELECT COLUMN_NAME, EXTRA
                      FROM information_schema.COLUMNS
                     WHERE TABLE_SCHEMA = %s
                       AND TABLE_NAME = %s
                     ORDER BY ORDINAL_POSITION
                    """,
                    (environ["INGEST_TEST_SOURCE_DB_NAME"], table),
                )
                columns = _writable_column_names(list(source_cursor.fetchall()))
                if not columns:
                    raise RuntimeError(f"source table has no writable columns: {table}")
                projection = ",".join(_quote_identifier(column) for column in columns)
            with target.cursor() as target_cursor:
                target_cursor.execute(f"DROP TABLE IF EXISTS `{target_db}`.`{table}`")
                target_cursor.execute(f"USE `{target_db}`")
                target_cursor.execute(create_sql)
            with source.cursor() as source_cursor:
                source_cursor.execute(
                    f"SELECT {projection} FROM {_quote_identifier(table)}"
                )
                placeholders = ",".join(["%s"] * len(columns))
                insert_sql = (
                    f"INSERT INTO {_quote_identifier(target_db)}."
                    f"{_quote_identifier(table)} ({projection}) "
                    f"VALUES ({placeholders})"
                )
                while True:
                    rows = source_cursor.fetchmany(1000)
                    if not rows:
                        break
                    with target.cursor() as target_cursor:
                        target_cursor.executemany(insert_sql, rows)
                    row_count += len(rows)
            table_count += 1
        source.rollback()
    finally:
        source.close()
        target.close()
    return {"tables": table_count, "rows": row_count}


def _execute_pipeline(
    *,
    manifest: Path,
    run_id: str,
    job_name: str,
    on_stage: Callable[[str, str], None],
) -> int:
    return stage_log_runner.run(
        manifest=manifest,
        run_id=run_id,
        job_name=job_name,
        on_stage=on_stage,
    )


def read_pipeline_observation(ledger_path: Path, run_id: str) -> dict:
    """Read durable stage timing and row-count evidence for one test execution."""
    with sqlite3.connect(ledger_path) as connection:
        stage_rows = connection.execute(
            """
            SELECT seq, stage, status, reason, started_at, finished_at, duration_ms
              FROM ingest_stage_event
             WHERE run_id = ?
             ORDER BY seq
            """,
            (run_id,),
        ).fetchall()
        ledger_row = connection.execute(
            """
            SELECT row_counts
              FROM ingest_ledger
             WHERE run_id = ?
             ORDER BY id DESC
             LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    stages = [
        {
            "seq": int(row[0]),
            "stage": str(row[1]),
            "status": str(row[2]),
            "reason": str(row[3]) if row[3] is not None else None,
            "started_at": str(row[4]) if row[4] is not None else None,
            "finished_at": str(row[5]) if row[5] is not None else None,
            "duration_ms": int(row[6]) if row[6] is not None else None,
        }
        for row in stage_rows
    ]
    row_counts = json.loads(str(ledger_row[0])) if ledger_row and ledger_row[0] else {}
    loaded_rows = sum(
        int(value)
        for value in row_counts.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    )
    for stage in stages:
        if stage["stage"] == "load":
            stage["rows"] = loaded_rows
    return {"stages": stages, "row_counts": row_counts}


def run_test_load(
    *,
    manifest: Path,
    run_id: str,
    job_name: str,
    environ: dict[str, str] | None = None,
    lifecycle_root: Path = Path("/lifecycle"),
    clone_snapshot: Callable[[dict[str, str]], dict] = clone_operating_snapshot,
    prepare_catalog: Callable[[dict[str, str]], dict] = _prepare_shadow_catalog,
    execute_pipeline: Callable[..., int] = _execute_pipeline,
    build_census: Callable[[dict[str, str]], dict] = build_change_census,
    read_observation: Callable[[Path, str], dict] = read_pipeline_observation,
) -> int:
    env = dict(os.environ if environ is None else environ)
    store = TestRunStore(Path(env["INGEST_TEST_RUN_ROOT"]))
    outcome = "failed"
    started = time.monotonic()
    live_stages: dict[str, dict[str, str]] = {}

    def record_live_stage(stage: str, status: str) -> None:
        live_stages[stage] = {"stage": stage, "status": status}
        store.update(
            run_id,
            current_stage=stage if status == "running" else None,
            stages=tuple(live_stages.values()),
        )

    try:
        validate_isolation_env(env)
        store.update(run_id, status="running", current_stage="snapshot")
        snapshot_started = time.monotonic()
        snapshot = clone_snapshot(env)
        catalog = prepare_catalog(env)
        snapshot = {**snapshot, "catalog_files": int(catalog["files"])}
        snapshot_seconds = round(time.monotonic() - snapshot_started, 3)

        store.update(run_id, current_stage="pipeline")
        pipeline_started = time.monotonic()
        exit_code = execute_pipeline(
            manifest=manifest,
            run_id=run_id,
            job_name=job_name,
            on_stage=record_live_stage,
        )
        pipeline_seconds = round(time.monotonic() - pipeline_started, 3)
        if exit_code != 0:
            raise RuntimeError(f"full ingest pipeline exited with {exit_code}")
        observation = read_observation(Path(env["INGEST_LEDGER_SQLITE"]), run_id)

        store.update(run_id, current_stage="census")
        census_started = time.monotonic()
        census = build_census(env)
        census_seconds = round(time.monotonic() - census_started, 3)
        result = {
            "snapshot": snapshot,
            "stages": observation["stages"],
            "row_counts": observation["row_counts"],
            "census": census,
            "timings_seconds": {
                "snapshot": snapshot_seconds,
                "pipeline": pipeline_seconds,
                "census": census_seconds,
                "total": round(time.monotonic() - started, 3),
            },
            "operating_database_writes": 0,
            "disposable_database": env["MARIADB_DATABASE"],
        }
        store.update(
            run_id,
            status="completed",
            current_stage=None,
            stages=tuple(observation["stages"]),
            result=result,
        )
        outcome = "completed"
        return 0
    except Exception as exc:  # fail closed and retain the reason for the portal.
        store.update(
            run_id,
            status="failed",
            current_stage=None,
            reason=f"{type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        lifecycle_root.mkdir(parents=True, exist_ok=True)
        (lifecycle_root / "done").write_text(f"{outcome}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job-name", required=True)
    args = parser.parse_args(argv)
    return run_test_load(
        manifest=args.manifest,
        run_id=args.run_id,
        job_name=args.job_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())
