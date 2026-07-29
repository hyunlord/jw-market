from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Final

import pymysql

from .topic_assignment_handoff_db import HANDOFF_TABLE


MIGRATION_PATH: Final = (
    Path(__file__).with_name("migrations") / "001_create_assignment_handoff.sql"
)
_CREATE_TABLE_RE: Final = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"`jw_brand_activity_stage`\.`([a-z0-9_]+)`",
    re.IGNORECASE,
)
_DESTRUCTIVE_RE: Final = re.compile(
    r"\b(DROP|ALTER|TRUNCATE|RENAME)\s+TABLE\b",
    re.IGNORECASE,
)
_DATA_STATEMENT_RE: Final = re.compile(
    r"(?:^|;)\s*(INSERT|UPDATE|DELETE|REPLACE)\b",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class MigrationValidation:
    """Static proof that the committed migration is additive and table-local."""

    table: str
    active_destructive_statements: tuple[str, ...]
    data_statements: tuple[str, ...]


def active_migration_sql(sql: str) -> str:
    """Return executable SQL with operator-only line comments removed."""
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    ).strip()


def validate_migration_sql(sql: str) -> MigrationValidation:
    """Parse the one-table additive migration or fail closed."""
    active_sql = active_migration_sql(sql)
    tables = tuple(_CREATE_TABLE_RE.findall(active_sql))
    destructive = tuple(
        match.group(1).upper() for match in _DESTRUCTIVE_RE.finditer(active_sql)
    )
    data_statements = tuple(
        match.group(1).upper() for match in _DATA_STATEMENT_RE.finditer(active_sql)
    )
    if tables != (HANDOFF_TABLE,):
        raise ValueError(
            "migration must create exactly "
            f"jw_brand_activity_stage.{HANDOFF_TABLE}"
        )
    if destructive:
        raise ValueError(f"destructive statements found: {destructive}")
    if data_statements:
        raise ValueError(f"data statements found: {data_statements}")
    return MigrationValidation(
        table=tables[0],
        active_destructive_statements=destructive,
        data_statements=data_statements,
    )


def connect_from_env() -> pymysql.connections.Connection:
    """Open the migration connection from the standard DB environment."""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def apply_migration(
    connection: pymysql.connections.Connection,
    *,
    sql: str,
) -> None:
    """Apply the validated committed CREATE statement once."""
    validate_migration_sql(sql)
    with connection.cursor() as cursor:
        cursor.execute(active_migration_sql(sql))


def main(argv: list[str] | None = None) -> int:
    """Validate or apply the committed handoff migration."""
    parser = argparse.ArgumentParser(
        description="Validate or apply the assignment handoff migration"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    validation = validate_migration_sql(sql)
    if args.dry_run:
        print(
            "migration_dry_run=PASS "
            f"table={validation.table} file={MIGRATION_PATH.name}"
        )
        return 0
    with connect_from_env() as connection:
        apply_migration(connection, sql=sql)
    print(
        "migration_execute=PASS "
        f"table={validation.table} file={MIGRATION_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
