"""Pre-deploy check that the ledger tables the image requires already exist.

REQUIRE_SIGNAL_LEDGER_STRICT and REQUIRE_STAGE_LEDGER_STRICT default to 1, so
shipping any image turns strict observation on whether or not that was the point
of the deploy.  The runtime preflight then refuses to ingest when a table is
missing: the rollout succeeds and ingestion stops.  Nothing checks before the
image moves, so the ordering between "apply the DDL" and "deploy" is left to
whoever happens to deploy next -- including a deploy made for an unrelated
reason.

This module is that missing check.  It is deliberately a precondition, not a
policy: there is no flag to switch it off, because "the table is missing" is a
fact about the target database and not an opinion about risk.

The expected column set is parsed from the committed DDL under
deploy/k8s/ingest-hook/reference/ rather than restated here, so the gate cannot
drift away from the file an operator would actually apply.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_REFERENCE_DIR = (
    Path(__file__).resolve().parents[3]
    / "deploy" / "k8s" / "ingest-hook" / "reference"
)

# table -> the DDL file an operator applies to create it
REQUIRED_TABLES: dict[str, str] = {
    "ingest_ledger": "ingest-ledger.sql",
    "ingest_stage_event": "ingest-stage-event.sql",
    "ingest_signal_event": "ingest-signal-event.sql",
    "ingest_status_transition": "ingest-status-transition.sql",
}

_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\(",
    re.IGNORECASE,
)
# A column definition line starts with the column name; table-level clauses do not.
_NON_COLUMN = re.compile(
    r"^\s*(PRIMARY\s+KEY|UNIQUE\s+KEY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN\s+KEY)\b",
    re.IGNORECASE,
)
_COLUMN_RE = re.compile(r"^\s*`?(\w+)`?\s+\S")


class DeployGateError(RuntimeError):
    """The gate could not prove the target database is ready for this image."""


@dataclass(frozen=True)
class TableVerdict:
    table: str
    present: bool
    missing_columns: tuple[str, ...]
    unexpected_columns: tuple[str, ...]
    ddl_file: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.present
            and not self.missing_columns
            and not self.unexpected_columns
            and self.error is None
        )

    def describe(self) -> str:
        if self.error is not None:
            # Unknown is not the same as fine; say which one this is.
            return f"{self.table}: UNKNOWN — schema unreadable: {self.error}"
        if not self.present:
            return f"{self.table}: MISSING — apply {self.ddl_file}"
        parts = []
        if self.missing_columns:
            parts.append(f"missing columns {list(self.missing_columns)}")
        if self.unexpected_columns:
            parts.append(f"unexpected columns {list(self.unexpected_columns)}")
        if parts:
            return f"{self.table}: SCHEMA MISMATCH — {'; '.join(parts)} (see {self.ddl_file})"
        return f"{self.table}: ok"


def expected_columns(table: str, *, reference_dir: Path | None = None) -> tuple[str, ...]:
    """Column names for `table`, read from the DDL an operator would apply."""
    directory = reference_dir or _REFERENCE_DIR
    ddl_path = directory / REQUIRED_TABLES[table]
    text = ddl_path.read_text(encoding="utf-8")
    match = _CREATE_RE.search(text)
    if match is None or match.group(1) != table:
        raise DeployGateError(f"cannot parse CREATE TABLE {table} from {ddl_path}")
    # Walk from the opening paren to its match; the reference DDL ends in ");"
    # with no ENGINE clause, so the body cannot be delimited by a trailing token.
    body_chars: list[str] = []
    depth = 1
    for char in text[match.end():]:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        body_chars.append(char)
    if depth != 0:
        raise DeployGateError(f"unbalanced CREATE TABLE body for {table} in {ddl_path}")

    columns: list[str] = []
    depth = 0
    current = ""
    for char in body_chars:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            columns.append(current)
            current = ""
        else:
            current += char
    columns.append(current)
    names: list[str] = []
    for line in columns:
        if not line.strip() or _NON_COLUMN.match(line):
            continue
        found = _COLUMN_RE.match(line)
        if found:
            names.append(found.group(1))
    if not names:
        raise DeployGateError(f"no columns parsed for {table} from {ddl_path}")
    return tuple(names)


def _live_columns(cursor, database: str, table: str) -> tuple[str, ...] | None:
    cursor.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"
        " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
        " ORDER BY ORDINAL_POSITION",
        (database, table),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return tuple(
        str(row["COLUMN_NAME"] if isinstance(row, Mapping) else row[0]) for row in rows
    )


def check_tables(cursor, database: str, *, reference_dir: Path | None = None) -> list[TableVerdict]:
    """One verdict per required table. Never raises for a per-table problem."""
    verdicts: list[TableVerdict] = []
    for table, ddl_file in REQUIRED_TABLES.items():
        try:
            expected = expected_columns(table, reference_dir=reference_dir)
            live = _live_columns(cursor, database, table)
        except Exception as exc:  # noqa: BLE001 — recorded as UNKNOWN, not as ok
            verdicts.append(
                TableVerdict(table, False, (), (), ddl_file, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        if live is None:
            verdicts.append(TableVerdict(table, False, (), (), ddl_file))
            continue
        verdicts.append(
            TableVerdict(
                table=table,
                present=True,
                missing_columns=tuple(c for c in expected if c not in live),
                unexpected_columns=tuple(c for c in live if c not in expected),
                ddl_file=ddl_file,
            )
        )
    return verdicts


def render(verdicts: list[TableVerdict], database: str) -> str:
    lines = [f"deploy gate: ledger tables in {database}"]
    lines.extend(f"  {verdict.describe()}" for verdict in verdicts)
    blockers = [verdict for verdict in verdicts if not verdict.ok]
    if not blockers:
        lines.append("VERDICT: ok — all required tables present and matching")
        return "\n".join(lines)
    lines.append(f"VERDICT: BLOCKED — {len(blockers)} table(s) not proven")
    # Only name a DDL to apply for tables we positively know are absent. A table
    # we could not read might already exist; telling someone to create it would
    # be advice derived from ignorance.
    files = sorted(
        verdict.ddl_file
        for verdict in blockers
        if not verdict.present and verdict.error is None
    )
    if files:
        lines.append(f"apply first: {', '.join(files)}")
    lines.append(
        "the image defaults REQUIRE_SIGNAL_LEDGER_STRICT and "
        "REQUIRE_STAGE_LEDGER_STRICT to 1; deploying now stops ingestion at preflight"
    )
    return "\n".join(lines)


def _default_connect():
    from pipeline.scripts.ingest_hook import config

    return config.open_mart_connection()


def main(argv: list[str] | None = None, *, connect: Callable = _default_connect) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="target schema; defaults to the MARIADB_DATABASE env")
    parser.add_argument("--reference-dir", help="override the committed DDL directory")
    args = parser.parse_args(argv)
    # Intentionally no --skip/--force: this checks a precondition, not a policy.

    try:
        connection = connect()
    except Exception as exc:  # noqa: BLE001
        # Cannot read the schema => cannot say it is ready. Block.
        print(f"deploy gate: BLOCKED — cannot reach the target database: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        database = args.database
        if not database:
            from pipeline.scripts.utils.mart_config import resolve_mart_db_name

            database = resolve_mart_db_name("MARIADB_DATABASE", "DB_NAME")
        with connection.cursor() as cursor:
            verdicts = check_tables(
                cursor,
                database,
                reference_dir=Path(args.reference_dir) if args.reference_dir else None,
            )
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 — close failure must not mask the verdict
            pass

    print(render(verdicts, database))
    return 0 if all(verdict.ok for verdict in verdicts) else 3


if __name__ == "__main__":
    raise SystemExit(main())
