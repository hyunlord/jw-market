"""AST and SQLite-authorizer enforcement for scoped SELECT queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Final

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .errors import FileSqlRejectedError

QUERY_TABLE: Final = "data"
ALLOWED_FUNCTIONS: Final = frozenset(
    {
        "abs",
        "avg",
        "coalesce",
        "count",
        "date",
        "datetime",
        "length",
        "like",
        "lower",
        "ltrim",
        "max",
        "min",
        "nullif",
        "round",
        "rtrim",
        "strftime",
        "substr",
        "substring",
        "sum",
        "trim",
        "upper",
    }
)
FORBIDDEN_NODE_NAMES: Final = frozenset(
    {
        "Alter",
        "Attach",
        "Command",
        "Create",
        "Delete",
        "Detach",
        "Drop",
        "Insert",
        "Merge",
        "Pragma",
        "Transaction",
        "Update",
    }
)


def validate_and_rewrite_query(sql: str, physical_table: str) -> str:
    """Parse one SELECT statement, enforce policy, and resolve its logical table."""
    try:
        statements = [statement for statement in parse(sql, read="sqlite") if statement]
    except ParseError as exc:
        raise FileSqlRejectedError("query is not valid SQLite SELECT syntax") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise FileSqlRejectedError("only one SELECT statement is allowed")

    statement = statements[0]
    for node in statement.walk():
        if type(node).__name__ in FORBIDDEN_NODE_NAMES:
            raise FileSqlRejectedError("query contains a forbidden SQL operation")

    tables = tuple(statement.find_all(exp.Table))
    if not tables:
        raise FileSqlRejectedError("query must read the session table")
    for table in tables:
        if table.db or table.catalog or table.name.lower() != QUERY_TABLE:
            raise FileSqlRejectedError("query may reference only the logical table 'data'")
        table.set("this", exp.to_identifier(physical_table))

    for function in statement.find_all(exp.Func):
        if isinstance(function, exp.Connector):
            continue
        function_name = function.sql_name().lower()
        if function_name not in ALLOWED_FUNCTIONS:
            raise FileSqlRejectedError(f"function is not allowed: {function_name}")
    return statement.sql(dialect="sqlite")


def _sqlite_authorizer(
    physical_table: str,
    allowed_functions: frozenset[str] = ALLOWED_FUNCTIONS,
) -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    """Build an engine-level allowlist independent of the AST policy."""

    def authorize(
        action: int,
        arg1: str | None,
        arg2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return (
                sqlite3.SQLITE_OK
                if arg1 == physical_table and arg1 not in {"sqlite_master", "sqlite_schema"}
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (arg2 or arg1 or "").lower()
            return (
                sqlite3.SQLITE_OK
                if function_name in allowed_functions
                else sqlite3.SQLITE_DENY
            )
        return sqlite3.SQLITE_DENY

    return authorize
