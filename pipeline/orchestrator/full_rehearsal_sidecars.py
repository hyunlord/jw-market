"""Prepare serving sidecar tables inside an isolated full rehearsal schema."""

from __future__ import annotations

import argparse
import re

import pymysql

from pipeline.scripts.deploy.analysis_cache_db import connect_admin
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_exists


MART_PREFIX = "jw_mart_rehearsal_"
MALB_TABLE = "mart_analysis_level_block"
SAFE_DB_RE = re.compile(r"^[A-Za-z0-9_]+$")


def prepare_malb_table(
    conn: pymysql.connections.Connection,
    *,
    reference_db: str,
    target_db: str,
) -> str:
    """Create an empty MALB table in an isolated schema from canonical DDL."""
    _validate_db_name("reference_db", reference_db)
    _validate_db_name("target_db", target_db)
    if not target_db.startswith(MART_PREFIX):
        raise ValueError(f"target_db must start with {MART_PREFIX!r}: {target_db!r}")
    if reference_db == target_db:
        raise ValueError("reference_db and target_db must differ")
    if not table_exists(conn, reference_db, MALB_TABLE):
        raise RuntimeError(f"reference table missing: {reference_db}.{MALB_TABLE}")
    if table_exists(conn, target_db, MALB_TABLE):
        raise RuntimeError(f"target table already exists: {target_db}.{MALB_TABLE}")

    statement = (
        f"CREATE TABLE {quote_id(target_db)}.{quote_id(MALB_TABLE)} "
        f"LIKE {quote_id(reference_db)}.{quote_id(MALB_TABLE)}"
    )
    with conn.cursor() as cursor:
        cursor.execute(statement)
    return statement


def _validate_db_name(label: str, value: str) -> None:
    if not SAFE_DB_RE.fullmatch(value):
        raise ValueError(f"unsafe {label}: {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-db", required=True)
    parser.add_argument("--target-db", required=True)
    args = parser.parse_args(argv)

    conn = connect_admin()
    try:
        statement = prepare_malb_table(
            conn,
            reference_db=args.reference_db,
            target_db=args.target_db,
        )
    finally:
        conn.close()
    print(statement)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
