#!/usr/bin/env python3
"""Apply MariaDB SQL migrations for the local mart database."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pymysql

from ops_utils import configure_logging, find_project_root, first_existing, retry


DEFAULT_DB = "jw_mart"
LOGGER = configure_logging(__name__)


ROOT = find_project_root(Path(__file__).resolve())
MIGRATIONS_DIR = first_existing(ROOT / "pipeline" / "migrations", ROOT / "migrations")
ENV_PATH = first_existing(ROOT / "pipeline" / "docker" / ".env", ROOT / "docker" / ".env")


@dataclass(frozen=True)
class Migration:
    migration_id: str
    path: Path
    checksum: str
    description: str


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing env file: {path}")

    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@retry((pymysql.err.OperationalError, pymysql.err.InterfaceError), logger=LOGGER)
def connect():
    env = load_env(ENV_PATH)
    user = env.get("MARIADB_USER", "jwapp")
    password = env.get("MARIADB_PASSWORD")
    port = int(env.get("HOST_PORT", "3307"))
    database = env.get("MARIADB_DATABASE", DEFAULT_DB)
    if not password:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {ENV_PATH}")

    return pymysql.connect(
        host="127.0.0.1",
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_migrations() -> list[Migration]:
    if not MIGRATIONS_DIR.exists():
        raise FileNotFoundError(f"Missing migrations directory: {MIGRATIONS_DIR}")
    migrations: list[Migration] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.match(r"^(\d{3})_(.+)\.sql$", path.name)
        if not match:
            continue
        migrations.append(
            Migration(
                migration_id=match.group(1),
                path=path,
                checksum=checksum(path),
                description=match.group(2).replace("_", " "),
            )
        )
    return migrations


def selected_migrations(selection: str) -> list[Migration]:
    migrations = list_migrations()
    if selection == "--all":
        return migrations
    chosen = [m for m in migrations if m.migration_id == selection]
    if not chosen:
        known = ", ".join(m.migration_id for m in migrations) or "(none)"
        raise RuntimeError(f"Unknown migration id: {selection}. Known: {known}")
    return chosen


def split_sql(sql: str) -> list[str]:
    sql = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escape = False

    for char in sql:
        current.append(char)
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement[:-1].strip())
            current = []

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return [stmt for stmt in statements if stmt and not stmt.startswith("--")]


def ensure_state_table(conn) -> None:
    state_path = MIGRATIONS_DIR / "000_migration_state.sql"
    sql = state_path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in split_sql(sql):
            cur.execute(stmt)
    conn.commit()


def applied_migrations(conn) -> dict[str, str]:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT migration_id, checksum FROM _migration_state")
            return {row["migration_id"]: row["checksum"] for row in cur.fetchall()}
    except pymysql.err.ProgrammingError:
        return {}


def apply_migration(conn, migration: Migration) -> str:
    if migration.migration_id != "000":
        ensure_state_table(conn)

    applied = applied_migrations(conn)
    if migration.migration_id in applied:
        if applied[migration.migration_id] != migration.checksum:
            raise RuntimeError(
                f"Checksum mismatch for already-applied migration {migration.migration_id}"
            )
        return "SKIP"

    sql = migration.path.read_text(encoding="utf-8")
    statements = split_sql(sql)
    LOGGER.info("Applying %s (%s statements)", migration.path.name, len(statements))

    try:
        with conn.cursor() as cur:
            for idx, stmt in enumerate(statements, start=1):
                preview = " ".join(stmt.split())[:120]
                LOGGER.info("[%s/%s] %s", idx, len(statements), preview)
                cur.execute(stmt)

            if migration.migration_id == "000":
                # The state table is created by the migration itself.
                pass
            else:
                ensure_state_table(conn)

            cur.execute(
                """
                INSERT INTO _migration_state (migration_id, checksum, description)
                VALUES (%s, %s, %s)
                """,
                (migration.migration_id, migration.checksum, migration.description),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return "APPLIED"


def command_status() -> int:
    migrations = list_migrations()
    with connect() as conn:
        applied = applied_migrations(conn)

    print("migration_id\tstatus\tdescription")
    for migration in migrations:
        state = "pending"
        if migration.migration_id in applied:
            state = "applied" if applied[migration.migration_id] == migration.checksum else "changed"
        print(f"{migration.migration_id}\t{state}\t{migration.description}")
    return 0


def command_apply(selection: str) -> int:
    migrations = selected_migrations(selection)
    with connect() as conn:
        for migration in migrations:
            result = apply_migration(conn, migration)
            LOGGER.info("%s: %s", migration.migration_id, result)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show migration state")
    apply_parser = sub.add_parser("apply", help="Apply a migration")
    apply_parser.add_argument("migration_id", help="Migration id, e.g. 001, or --all")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            return command_status()
        if args.command == "apply":
            return command_apply(args.migration_id)
    except Exception as exc:
        LOGGER.error("ERROR: %s", exc)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
