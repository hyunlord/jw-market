from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("sql") / "001_create_hira_benefit_tables.sql"
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?([a-z0-9_]+)`?",
    re.IGNORECASE,
)
_DESTRUCTIVE_RE = re.compile(
    r"\b(?:(DROP|TRUNCATE|RENAME)\s+TABLE|(DELETE)\s+FROM)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SchemaValidation:
    tables: tuple[str, ...]
    destructive_statements: tuple[str, ...]


def validate_schema_sql(sql: str) -> SchemaValidation:
    tables = tuple(_CREATE_TABLE_RE.findall(sql))
    destructive = tuple(
        next(group for group in match.groups() if group).upper()
        for match in _DESTRUCTIVE_RE.finditer(sql)
    )
    if len(tables) != len(set(tables)):
        raise ValueError("schema declares duplicate table names")
    return SchemaValidation(tables=tables, destructive_statements=destructive)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate HIRA DDL without applying it")
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.parse_args(argv)
    result = validate_schema_sql(SCHEMA_PATH.read_text(encoding="utf-8"))
    if result.destructive_statements:
        raise SystemExit(
            f"destructive statements found: {result.destructive_statements}"
        )
    print(f"schema_dry_run=PASS tables={','.join(result.tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
