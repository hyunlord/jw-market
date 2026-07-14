"""Prepare, validate, switch, and roll back Brand Activity topic marts."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import time
from typing import Final

import pymysql

from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store_db import (
    RUNS_TABLE,
    STAGING_RUNS_TABLE,
    STAGING_TOPICS_TABLE,
    TOPICS_TABLE,
)
from pipeline.scripts.deploy.analysis_cache_db import connect_admin, validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_exists


LIVE_TABLES: Final[tuple[str, str]] = (TOPICS_TABLE, RUNS_TABLE)
STAGING_TABLES: Final[dict[str, str]] = {
    TOPICS_TABLE: STAGING_TOPICS_TABLE,
    RUNS_TABLE: STAGING_RUNS_TABLE,
}


@dataclass(frozen=True, slots=True)
class StagingValidation:
    topic_rows: int
    topic_brand_count: int
    run_rows: int
    invalid_json_rows: int
    stale_topic_rows: int = 0
    stale_run_rows: int = 0


@dataclass(frozen=True, slots=True)
class SwapSummary:
    action: str
    target_db: str
    run_id: str
    elapsed_seconds: float
    validation: StagingValidation | None = None


def prepare_staging_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
) -> tuple[str, ...]:
    validate_schema_name("target_db", target_db)
    for live, staging in STAGING_TABLES.items():
        if not table_exists(conn, target_db, live):
            raise RuntimeError(f"live table missing: {target_db}.{live}")
        if table_exists(conn, target_db, staging):
            raise RuntimeError(f"staging table already exists: {target_db}.{staging}")
    statements = tuple(
        f"CREATE TABLE {quote_id(target_db)}.{quote_id(staging)} "
        f"LIKE {quote_id(target_db)}.{quote_id(live)}"
        for live, staging in STAGING_TABLES.items()
    )
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    return statements


def validate_staging_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    expected_topic_rows: int,
    expected_topic_brand_count: int,
    expected_topic_run_id: str,
) -> StagingValidation:
    validate_schema_name("target_db", target_db)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*) AS topic_rows,
                   COALESCE(SUM(JSON_LENGTH(JSON_EXTRACT(payload, '$.brands'))), 0) AS topic_brand_count,
                   COALESCE(SUM(NOT JSON_VALID(payload) OR NOT JSON_VALID(atc4_values)), 0) AS invalid_json_rows,
                   COALESCE(SUM(run_id <> %s), 0) AS stale_topic_rows
            FROM {quote_id(target_db)}.{quote_id(STAGING_TOPICS_TABLE)}
            """,
            (expected_topic_run_id,),
        )
        topic_row = _mapping_row(cursor.fetchone())
        cursor.execute(
            f"SELECT COUNT(*) AS run_rows, COALESCE(SUM(run_id <> %s), 0) AS stale_run_rows "
            f"FROM {quote_id(target_db)}.{quote_id(STAGING_RUNS_TABLE)}",
            (expected_topic_run_id,),
        )
        run_row = _mapping_row(cursor.fetchone())
    validation = StagingValidation(
        topic_rows=int(topic_row["topic_rows"]),
        topic_brand_count=int(topic_row["topic_brand_count"]),
        run_rows=int(run_row["run_rows"]),
        invalid_json_rows=int(topic_row["invalid_json_rows"]),
        stale_topic_rows=int(topic_row["stale_topic_rows"]),
        stale_run_rows=int(run_row["stale_run_rows"]),
    )
    expected = (expected_topic_rows, expected_topic_brand_count, 1, 0, 0, 0)
    actual = (
        validation.topic_rows,
        validation.topic_brand_count,
        validation.run_rows,
        validation.invalid_json_rows,
        validation.stale_topic_rows,
        validation.stale_run_rows,
    )
    if actual != expected:
        raise RuntimeError(f"topic staging census mismatch: expected={expected}, actual={actual}")
    return validation


def switch_blue_green_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    run_id: str,
    expected_topic_rows: int,
    expected_topic_brand_count: int,
    expected_topic_run_id: str,
) -> SwapSummary:
    _validate_identity(target_db=target_db, run_id=run_id)
    validation = validate_staging_tables(
        conn,
        target_db=target_db,
        expected_topic_rows=expected_topic_rows,
        expected_topic_brand_count=expected_topic_brand_count,
        expected_topic_run_id=expected_topic_run_id,
    )
    backups = _versioned_tables("old", run_id)
    _require_switch_tables(conn, target_db=target_db, backups=backups)
    statement = _switch_statement(target_db=target_db, backups=backups)
    started = time.perf_counter()
    with conn.cursor() as cursor:
        cursor.execute(statement)
    return SwapSummary(
        action="switch",
        target_db=target_db,
        run_id=run_id,
        elapsed_seconds=round(time.perf_counter() - started, 6),
        validation=validation,
    )


def rollback_blue_green_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    run_id: str,
) -> SwapSummary:
    _validate_identity(target_db=target_db, run_id=run_id)
    backups = _versioned_tables("old", run_id)
    failed = _versioned_tables("failed", run_id)
    for live in LIVE_TABLES:
        if not table_exists(conn, target_db, live):
            raise RuntimeError(f"live table missing: {target_db}.{live}")
        if not table_exists(conn, target_db, backups[live]):
            raise RuntimeError(f"rollback backup missing: {target_db}.{backups[live]}")
        if table_exists(conn, target_db, failed[live]):
            raise RuntimeError(f"rollback failed table already exists: {target_db}.{failed[live]}")
    moves: list[str] = []
    for live in LIVE_TABLES:
        moves.extend(
            (
                f"{quote_id(target_db)}.{quote_id(live)} TO {quote_id(target_db)}.{quote_id(failed[live])}",
                f"{quote_id(target_db)}.{quote_id(backups[live])} TO {quote_id(target_db)}.{quote_id(live)}",
            )
        )
    started = time.perf_counter()
    with conn.cursor() as cursor:
        cursor.execute("RENAME TABLE " + ", ".join(moves))
    return SwapSummary(
        action="rollback",
        target_db=target_db,
        run_id=run_id,
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )


def _mapping_row(row: object) -> dict[str, object]:
    if not isinstance(row, dict):
        raise RuntimeError(f"expected dictionary cursor row, got {type(row).__name__}")
    return row


def _validate_identity(*, target_db: str, run_id: str) -> None:
    validate_schema_name("target_db", target_db)
    validate_schema_name("run_id", run_id)


def _versioned_tables(label: str, run_id: str) -> dict[str, str]:
    tables = {table: f"{table}_{label}_{run_id}" for table in LIVE_TABLES}
    oversized = [table for table in tables.values() if len(table) > 64]
    if oversized:
        raise ValueError(f"generated MySQL identifier exceeds 64 characters: {oversized}")
    return tables


def _require_switch_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    backups: dict[str, str],
) -> None:
    for live, staging in STAGING_TABLES.items():
        if not table_exists(conn, target_db, live):
            raise RuntimeError(f"live table missing: {target_db}.{live}")
        if not table_exists(conn, target_db, staging):
            raise RuntimeError(f"staging table missing: {target_db}.{staging}")
        if table_exists(conn, target_db, backups[live]):
            raise RuntimeError(f"backup table already exists: {target_db}.{backups[live]}")


def _switch_statement(*, target_db: str, backups: dict[str, str]) -> str:
    moves: list[str] = []
    for live, staging in STAGING_TABLES.items():
        moves.extend(
            (
                f"{quote_id(target_db)}.{quote_id(live)} TO {quote_id(target_db)}.{quote_id(backups[live])}",
                f"{quote_id(target_db)}.{quote_id(staging)} TO {quote_id(target_db)}.{quote_id(live)}",
            )
        )
    return "RENAME TABLE " + ", ".join(moves)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("prepare")
    for action in ("validate", "switch"):
        command = subparsers.add_parser(action)
        command.add_argument("--expected-topic-rows", type=int, required=True)
        command.add_argument("--expected-topic-brand-count", type=int, required=True)
        command.add_argument("--expected-topic-run-id", required=True)
        if action == "switch":
            command.add_argument("--run-id", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--run-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = connect_admin()
    try:
        if args.action == "prepare":
            payload: object = {
                "action": "prepare",
                "target_db": args.target_db,
                "statements": prepare_staging_tables(conn, target_db=args.target_db),
            }
        elif args.action == "validate":
            payload = asdict(
                validate_staging_tables(
                    conn,
                    target_db=args.target_db,
                    expected_topic_rows=args.expected_topic_rows,
                    expected_topic_brand_count=args.expected_topic_brand_count,
                    expected_topic_run_id=args.expected_topic_run_id,
                )
            )
        elif args.action == "switch":
            payload = asdict(
                switch_blue_green_tables(
                    conn,
                    target_db=args.target_db,
                    run_id=args.run_id,
                    expected_topic_rows=args.expected_topic_rows,
                    expected_topic_brand_count=args.expected_topic_brand_count,
                    expected_topic_run_id=args.expected_topic_run_id,
                )
            )
        else:
            payload = asdict(
                rollback_blue_green_tables(conn, target_db=args.target_db, run_id=args.run_id)
            )
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
