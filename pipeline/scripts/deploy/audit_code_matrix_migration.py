#!/usr/bin/env python3
"""Backfill IQVIA audit_code_matrix without rebuilding general mart rows.

This operational helper intentionally updates only
``mart_general_brand_metric.audit_code_matrix`` for ``source='iqvia_nsa'``.
It reuses the canonical Layer 3 general mart builder to make the matrices, then
applies them in small Galera-safe batches so existing row identity and protected
columns stay untouched.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import pymysql

from pipeline.etl.io.mart.general_compute import compute_general
from pipeline.etl.io.mart.general_json import dumps
from pipeline.scripts.deploy.mart_load_ops import PROTECTED_TARGETS, connect_admin, load_env_file, validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import quote_id


TABLE_NAME = "mart_general_brand_metric"
SOURCE = "iqvia_nsa"
MAX_BATCH_SIZE = 200
PROTECTED_COLUMNS = (
    "metric_history",
    "raw_value_history",
    "dimension_data",
    "by_dimension",
)


@dataclass(frozen=True, slots=True)
class AuditMatrixUpdate:
    brand_key: str
    atc4_code: str
    measure: str
    audit_code_matrix: str | None


@dataclass(frozen=True, slots=True)
class DbFingerprint:
    row_count: int
    checksum: int


def bounded_batch_size(value: int) -> int:
    if value <= 0:
        raise ValueError("batch size must be positive")
    return min(value, MAX_BATCH_SIZE)


def ensure_audit_code_matrix_column(conn: pymysql.connections.Connection, target_db: str) -> bool:
    """Add the JSON column if it is missing.  Returns True when DDL ran."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS column_count
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND COLUMN_NAME = %s
            """,
            (target_db, TABLE_NAME, "audit_code_matrix"),
        )
        row = cur.fetchone() or {}
        if int(row.get("column_count") or 0) > 0:
            return False
        cur.execute(
            f"""
            ALTER TABLE {quote_id(target_db)}.{quote_id(TABLE_NAME)}
              ADD COLUMN audit_code_matrix LONGTEXT NULL
              CHECK (audit_code_matrix IS NULL OR JSON_VALID(audit_code_matrix))
            """
        )
    return True


def protected_fingerprint(conn: pymysql.connections.Connection, target_db: str) -> DbFingerprint:
    """Return a stable fingerprint for columns this migration must not mutate."""

    pieces = [f"COALESCE(CAST({quote_id(column)} AS CHAR), '<NULL>')" for column in PROTECTED_COLUMNS]
    expression = "CONCAT_WS('\\x1f', " + ", ".join(pieces) + ")"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(CRC32({expression})), 0) AS checksum
            FROM {quote_id(target_db)}.{quote_id(TABLE_NAME)}
            WHERE source = %s
            """,
            (SOURCE,),
        )
        row = cur.fetchone() or {}
    return DbFingerprint(row_count=int(row.get("row_count") or 0), checksum=int(row.get("checksum") or 0))


def invalid_json_count(conn: pymysql.connections.Connection, target_db: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS invalid_count
            FROM {quote_id(target_db)}.{quote_id(TABLE_NAME)}
            WHERE source = %s
              AND audit_code_matrix IS NOT NULL
              AND JSON_VALID(audit_code_matrix) = 0
            """,
            (SOURCE,),
        )
        row = cur.fetchone() or {}
    return int(row.get("invalid_count") or 0)


def build_update_plan(
    *,
    limit_atc4: int | None,
    max_rows: int | None,
    output_dir: Path,
) -> tuple[list[AuditMatrixUpdate], dict[str, Any]]:
    brand_rows, _market_rows, stats = compute_general(
        source=SOURCE,
        dry_run=True,
        insert=False,
        limit_atc4=limit_atc4,
        max_rows=max_rows,
        output_dir=output_dir,
    )
    updates: list[AuditMatrixUpdate] = []
    for row in brand_rows:
        matrix = row.get("audit_code_matrix") or {}
        updates.append(
            AuditMatrixUpdate(
                brand_key=str(row["brand_key"]),
                atc4_code=str(row["atc4_code"]),
                measure=str(row["measure"]),
                audit_code_matrix=dumps(matrix) if matrix else None,
            )
        )
    return updates, stats


def _chunks(values: Sequence[AuditMatrixUpdate], batch_size: int) -> Iterable[Sequence[AuditMatrixUpdate]]:
    capped = bounded_batch_size(batch_size)
    for start in range(0, len(values), capped):
        yield values[start : start + capped]


def update_audit_code_matrices(
    conn: pymysql.connections.Connection,
    target_db: str,
    updates: Sequence[AuditMatrixUpdate],
    *,
    batch_size: int = MAX_BATCH_SIZE,
) -> int:
    sql = (
        f"UPDATE {quote_id(target_db)}.{quote_id(TABLE_NAME)} "
        "SET audit_code_matrix = %s "
        "WHERE source = %s AND brand_key = %s AND atc4_code = %s AND measure = %s"
    )
    updated = 0
    for batch in _chunks(updates, batch_size):
        payload = [
            (item.audit_code_matrix, SOURCE, item.brand_key, item.atc4_code, item.measure)
            for item in batch
        ]
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
        updated += len(payload)
    return updated


def rollback_audit_code_matrices(
    conn: pymysql.connections.Connection,
    target_db: str,
    *,
    batch_size: int = MAX_BATCH_SIZE,
) -> int:
    """Set IQVIA matrices back to NULL in bounded primary-key batches."""

    capped = bounded_batch_size(batch_size)
    total = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT brand_key, atc4_code, measure
                FROM {quote_id(target_db)}.{quote_id(TABLE_NAME)}
                WHERE source = %s AND audit_code_matrix IS NOT NULL
                ORDER BY brand_key, atc4_code, measure
                LIMIT %s
                """,
                (SOURCE, capped),
            )
            rows = cur.fetchall()
        if not rows:
            break
        payload = [(row["brand_key"], row["atc4_code"], row["measure"]) for row in rows]
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                UPDATE {quote_id(target_db)}.{quote_id(TABLE_NAME)}
                SET audit_code_matrix = NULL
                WHERE source = %s AND brand_key = %s AND atc4_code = %s AND measure = %s
                """,
                [(SOURCE, brand_key, atc4_code, measure) for brand_key, atc4_code, measure in payload],
            )
        total += len(payload)
    return total


def guard_target(target_db: str, *, allow_operating_target: bool) -> None:
    validate_schema_name("target_db", target_db)
    if target_db in PROTECTED_TARGETS and not allow_operating_target:
        raise RuntimeError("refusing operating target without --allow-operating-target")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Optional MariaDB env file")
    parser.add_argument("--target-db", required=True, help="Target schema that already contains mart_general_brand_metric")
    parser.add_argument("--dry-run", action="store_true", help="Build the update plan without touching the target DB")
    parser.add_argument("--apply", action="store_true", help="Apply the audit_code_matrix update plan")
    parser.add_argument("--rollback-null", action="store_true", help="Set IQVIA audit_code_matrix values back to NULL")
    parser.add_argument("--allow-operating-target", action="store_true", help="Allow writes to protected operating schemas")
    parser.add_argument("--limit-atc4", type=int, default=None, help="Validation-only ATC4 limit")
    parser.add_argument("--max-rows", type=int, default=None, help="Validation-only raw-row limit")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/audit_code_matrix_migration"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    action_count = int(args.dry_run) + int(args.apply) + int(args.rollback_null)
    if action_count != 1:
        raise SystemExit("Choose exactly one of --dry-run, --apply, or --rollback-null")
    load_env_file(args.env_file)
    guard_target(args.target_db, allow_operating_target=args.allow_operating_target)
    batch_size = bounded_batch_size(args.batch_size)

    if args.dry_run:
        updates, stats = build_update_plan(limit_atc4=args.limit_atc4, max_rows=args.max_rows, output_dir=args.output_dir)
        print(
            json.dumps(
                {
                    "action": "dry-run",
                    "target_db": args.target_db,
                    "planned_updates": len(updates),
                    "nonempty_matrices": sum(1 for item in updates if item.audit_code_matrix),
                    "stats": stats,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    conn = connect_admin()
    try:
        if args.rollback_null:
            before = protected_fingerprint(conn, args.target_db)
            cleared = rollback_audit_code_matrices(conn, args.target_db, batch_size=batch_size)
            after = protected_fingerprint(conn, args.target_db)
            print(json.dumps({"action": "rollback-null", "cleared": cleared, "before": asdict(before), "after": asdict(after)}, indent=2))
            if before != after:
                raise RuntimeError("protected column fingerprint changed during rollback")
            return 0

        updates, stats = build_update_plan(limit_atc4=args.limit_atc4, max_rows=args.max_rows, output_dir=args.output_dir)
        column_added = ensure_audit_code_matrix_column(conn, args.target_db)
        before = protected_fingerprint(conn, args.target_db)
        updated = update_audit_code_matrices(conn, args.target_db, updates, batch_size=batch_size)
        after = protected_fingerprint(conn, args.target_db)
        invalid_count = invalid_json_count(conn, args.target_db)
        result = {
            "action": "apply",
            "target_db": args.target_db,
            "column_added": column_added,
            "planned_updates": len(updates),
            "updated": updated,
            "nonempty_matrices": sum(1 for item in updates if item.audit_code_matrix),
            "invalid_json_count": invalid_count,
            "before": asdict(before),
            "after": asdict(after),
            "stats": stats,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if before != after:
            raise RuntimeError("protected column fingerprint changed")
        if invalid_count:
            raise RuntimeError("audit_code_matrix contains invalid JSON")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
