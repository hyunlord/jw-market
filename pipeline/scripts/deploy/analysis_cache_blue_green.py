"""Prepare and atomically switch the analysis-block and brand caches.

The builder writes only to the two staging tables. A switch or rollback moves
both live identities in one ``RENAME TABLE`` statement so consumers cannot see
one generation of MALB with another generation of ``cache_brands``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import time
from typing import Final

import pymysql

from pipeline.scripts.api.dynamic_market.analysis_level_block_contract import (
    ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
)
from pipeline.scripts.deploy.analysis_cache_db import connect_admin, validate_schema_name
from pipeline.scripts.deploy.analysis_cache_blue_green_validation import (
    CACHE_BRANDS_TABLE,
    DEFAULT_EXPECTED_BRAND_COUNT,
    DEFAULT_EXPECTED_MALB_ROWS,
    MALB_TABLE,
    STAGING_TABLES,
    StagingValidation,
    validate_staging_tables,
)
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_exists
from pipeline.scripts.rollback.recording import (
    add_promotion_identity_args,
    identity_from_args,
    record_mysql_component,
)


BLUE_GREEN_PUBLISH_TABLES: Final[tuple[str, str]] = (
    MALB_TABLE,
    CACHE_BRANDS_TABLE,
)
LIVE_TABLES: Final[tuple[str, str]] = BLUE_GREEN_PUBLISH_TABLES


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
    for live_table, staging_table in STAGING_TABLES.items():
        if not table_exists(conn, target_db, live_table):
            raise RuntimeError(f"live table missing: {target_db}.{live_table}")
        if table_exists(conn, target_db, staging_table):
            raise RuntimeError(f"staging table already exists: {target_db}.{staging_table}")
    statements = tuple(
        f"CREATE TABLE {quote_id(target_db)}.{quote_id(staging_table)} "
        f"LIKE {quote_id(target_db)}.{quote_id(live_table)}"
        for live_table, staging_table in STAGING_TABLES.items()
    )
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    return statements


def switch_blue_green_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    run_id: str,
    expected_brands_sha256: str,
    expected_source_epoch: str,
    expected_malb_rows: int = DEFAULT_EXPECTED_MALB_ROWS,
    expected_brand_count: int = DEFAULT_EXPECTED_BRAND_COUNT,
    expected_build_version: str = ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
) -> SwapSummary:
    _validate_run_identity(target_db=target_db, run_id=run_id)
    validation = validate_staging_tables(
        conn,
        target_db=target_db,
        expected_brands_sha256=expected_brands_sha256,
        expected_malb_rows=expected_malb_rows,
        expected_brand_count=expected_brand_count,
        expected_source_epoch=expected_source_epoch,
        expected_build_version=expected_build_version,
    )
    old_tables = _versioned_tables("old", run_id)
    for live_table, staging_table in STAGING_TABLES.items():
        if not table_exists(conn, target_db, live_table):
            raise RuntimeError(f"live table missing: {target_db}.{live_table}")
        if not table_exists(conn, target_db, staging_table):
            raise RuntimeError(f"staging table missing: {target_db}.{staging_table}")
        if table_exists(conn, target_db, old_tables[live_table]):
            raise RuntimeError(
                f"backup table already exists: {target_db}.{old_tables[live_table]}"
            )

    statement = _switch_statement(target_db=target_db, old_tables=old_tables)
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
    _validate_run_identity(target_db=target_db, run_id=run_id)
    old_tables = _versioned_tables("old", run_id)
    failed_tables = _versioned_tables("failed", run_id)
    for live_table in LIVE_TABLES:
        if not table_exists(conn, target_db, live_table):
            raise RuntimeError(f"live table missing: {target_db}.{live_table}")
        if not table_exists(conn, target_db, old_tables[live_table]):
            raise RuntimeError(f"rollback backup missing: {target_db}.{old_tables[live_table]}")
        if table_exists(conn, target_db, failed_tables[live_table]):
            raise RuntimeError(
                f"rollback failed table already exists: {target_db}.{failed_tables[live_table]}"
            )

    statement = _rollback_statement(
        target_db=target_db,
        old_tables=old_tables,
        failed_tables=failed_tables,
    )
    started = time.perf_counter()
    with conn.cursor() as cursor:
        cursor.execute(statement)
    return SwapSummary(
        action="rollback",
        target_db=target_db,
        run_id=run_id,
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )


def _validate_run_identity(*, target_db: str, run_id: str) -> None:
    validate_schema_name("target_db", target_db)
    validate_schema_name("run_id", run_id)


def _versioned_tables(label: str, run_id: str) -> dict[str, str]:
    tables = {table: f"{table}_{label}_{run_id}" for table in LIVE_TABLES}
    oversized = [name for name in tables.values() if len(name) > 64]
    if oversized:
        raise ValueError(f"generated MySQL identifier exceeds 64 characters: {oversized}")
    return tables


def _switch_statement(*, target_db: str, old_tables: dict[str, str]) -> str:
    moves: list[str] = []
    for live_table, staging_table in STAGING_TABLES.items():
        moves.extend(
            (
                f"{quote_id(target_db)}.{quote_id(live_table)} TO "
                f"{quote_id(target_db)}.{quote_id(old_tables[live_table])}",
                f"{quote_id(target_db)}.{quote_id(staging_table)} TO "
                f"{quote_id(target_db)}.{quote_id(live_table)}",
            )
        )
    return "RENAME TABLE " + ", ".join(moves)


def _rollback_statement(
    *,
    target_db: str,
    old_tables: dict[str, str],
    failed_tables: dict[str, str],
) -> str:
    moves: list[str] = []
    for live_table in LIVE_TABLES:
        moves.extend(
            (
                f"{quote_id(target_db)}.{quote_id(live_table)} TO "
                f"{quote_id(target_db)}.{quote_id(failed_tables[live_table])}",
                f"{quote_id(target_db)}.{quote_id(old_tables[live_table])} TO "
                f"{quote_id(target_db)}.{quote_id(live_table)}",
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
        command.add_argument("--expected-brands-sha256", required=True)
        command.add_argument(
            "--expected-malb-rows",
            type=int,
            default=DEFAULT_EXPECTED_MALB_ROWS,
        )
        command.add_argument(
            "--expected-brand-count",
            type=int,
            default=DEFAULT_EXPECTED_BRAND_COUNT,
        )
        command.add_argument("--expected-source-epoch", required=True)
        command.add_argument(
            "--expected-build-version",
            default=ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
        )
        if action == "switch":
            command.add_argument("--run-id", required=True)
            add_promotion_identity_args(command)

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
                    expected_brands_sha256=args.expected_brands_sha256,
                    expected_malb_rows=args.expected_malb_rows,
                    expected_brand_count=args.expected_brand_count,
                    expected_source_epoch=args.expected_source_epoch,
                    expected_build_version=args.expected_build_version,
                )
            )
        elif args.action == "switch":
            identity = identity_from_args(
                args,
                promotion_run_id=args.run_id,
                serving_db=args.target_db,
            )
            summary = switch_blue_green_tables(
                    conn,
                    target_db=args.target_db,
                    run_id=args.run_id,
                    expected_brands_sha256=args.expected_brands_sha256,
                    expected_malb_rows=args.expected_malb_rows,
                    expected_brand_count=args.expected_brand_count,
                    expected_source_epoch=args.expected_source_epoch,
                    expected_build_version=args.expected_build_version,
                )
            if identity is not None:
                old_tables = _versioned_tables("old", args.run_id)
                record_mysql_component(
                    conn,
                    identity=identity,
                    component="analysis_cache",
                    table_pairs=tuple(
                        (live_table, old_tables[live_table]) for live_table in LIVE_TABLES
                    ),
                )
            payload = asdict(summary)
        else:
            payload = asdict(
                rollback_blue_green_tables(
                    conn,
                    target_db=args.target_db,
                    run_id=args.run_id,
                )
            )
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
