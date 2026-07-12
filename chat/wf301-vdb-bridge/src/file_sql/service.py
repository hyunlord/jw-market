"""Session-local SQLite provisioning and query service."""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Iterable, Sequence
from itertools import islice
from typing import Final

from .config import FileSqlConfig
from .errors import FileSqlDisabledError, FileSqlNotFoundError, FileSqlRejectedError
from .models import (
    FileSqlInventory,
    FileSqlResult,
    ProvisionedSchema,
    SqlValue,
    TableReference,
)
from .policy import QUERY_TABLE, _sqlite_authorizer, validate_and_rewrite_query
from .registry import UploadSqlRegistry

SQLITE_PROGRESS_OPCODES: Final = 1_000


class FileSqlService:
    """Owns a registry and exposes the only four supported file-SQL operations."""

    def __init__(
        self,
        config: FileSqlConfig | None = None,
        registry: UploadSqlRegistry | None = None,
    ) -> None:
        self.config = config or FileSqlConfig.from_env()
        self._registry = registry or UploadSqlRegistry()
        self._registry.bind_root(self.config.root_dir)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise FileSqlDisabledError("file SQL is disabled")

    def provision_session_table(
        self,
        session_id: str,
        logical_name: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[SqlValue]],
    ) -> ProvisionedSchema:
        self._require_enabled()
        source_columns = tuple(columns)
        if not source_columns:
            raise FileSqlRejectedError("at least one source column is required")
        reference = self._registry.allocate(
            self.config.root_dir, session_id, logical_name, source_columns
        )
        reference.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        create_sql = self._create_sql(reference)
        placeholders = ", ".join("?" for _ in reference.query_columns)
        insert_sql = f'INSERT INTO "{reference.physical_table}" VALUES ({placeholders})'
        row_iter = iter(rows)
        try:
            with sqlite3.connect(reference.database_path) as connection:
                connection.execute(create_sql)
                while batch := tuple(
                    tuple(row)
                    for row in islice(row_iter, self.config.insert_batch_rows)
                ):
                    if any(len(row) != len(source_columns) for row in batch):
                        raise FileSqlRejectedError(
                            "row width does not match the source schema"
                        )
                    connection.executemany(insert_sql, batch)
                connection.commit()
        except Exception:
            try:
                self._registry.remove_logical(session_id, logical_name)
            except FileSqlNotFoundError:
                pass
            try:
                self._drop_allocated_table(reference)
            except sqlite3.Error:
                pass
            raise
        reference.database_path.chmod(0o600)
        self._registry.persist(reference)
        return self._schema(reference, create_sql)

    def run_scoped_query(
        self, session_id: str, logical_name: str, sql: str
    ) -> FileSqlResult:
        self._require_enabled()
        reference = self._registry.resolve(session_id, logical_name)
        rewritten_sql = validate_and_rewrite_query(sql, reference.physical_table)
        uri = reference.database_path.resolve().as_uri() + "?mode=ro"
        deadline = time.monotonic() + self.config.query_timeout_seconds
        try:
            with sqlite3.connect(uri, uri=True, timeout=0.1) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.set_authorizer(_sqlite_authorizer(reference.physical_table))
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0,
                    SQLITE_PROGRESS_OPCODES,
                )
                cursor = connection.execute(rewritten_sql)
                rows = cursor.fetchmany(self.config.row_limit + 1)
                if len(rows) > self.config.row_limit:
                    raise FileSqlRejectedError("query exceeded the configured row cap")
                columns = tuple(item[0] for item in (cursor.description or ()))
        except sqlite3.Error as exc:
            raise FileSqlRejectedError("SQLite rejected or interrupted the query") from exc
        return FileSqlResult(columns=columns, rows=tuple(tuple(row) for row in rows))

    def drop_session_tables(self, session_id: str) -> None:
        self._require_enabled()
        references = self._registry.remove_session(session_id)
        if references:
            shutil.rmtree(references[0].database_path.parent, ignore_errors=True)

    def drop_logical_table(self, session_id: str, logical_name: str) -> None:
        self._require_enabled()
        reference = self._registry.remove_logical(session_id, logical_name)
        try:
            self._drop_allocated_table(reference)
        except sqlite3.Error as exc:
            raise FileSqlRejectedError("file SQL table cleanup failed") from exc

    def describe_schema_for_llm(
        self, session_id: str, logical_name: str
    ) -> ProvisionedSchema:
        self._require_enabled()
        reference = self._registry.resolve(session_id, logical_name)
        return self._schema(reference, self._create_sql(reference))

    def debug_session_inventory(self, session_id: str) -> FileSqlInventory:
        """Return sanitized engine inventory for penetration verification."""
        self._require_enabled()
        references = self._registry.references_for_session(session_id)
        if not references:
            raise FileSqlRejectedError("session has no registered tables")
        reference = references[0]
        uri = reference.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            database_names = tuple(row[1] for row in connection.execute("PRAGMA database_list"))
            table_names = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
                )
            )
        return FileSqlInventory(database_names, table_names, uri)

    @staticmethod
    def _create_sql(reference: TableReference) -> str:
        columns_sql = ", ".join(f'"{name}"' for name in reference.query_columns)
        return f'CREATE TABLE "{reference.physical_table}" ({columns_sql})'

    @staticmethod
    def _drop_allocated_table(reference: TableReference) -> None:
        if not reference.database_path.is_file():
            return
        with sqlite3.connect(reference.database_path) as connection:
            connection.execute(f'DROP TABLE IF EXISTS "{reference.physical_table}"')
            connection.commit()

    @staticmethod
    def _schema(reference: TableReference, physical_sql: str) -> ProvisionedSchema:
        column_pairs = ", ".join(
            f"{query} ({source})"
            for source, query in zip(
                reference.source_columns, reference.query_columns, strict=True
            )
        )
        return ProvisionedSchema(
            logical_name=reference.logical_name,
            query_table=QUERY_TABLE,
            source_columns=reference.source_columns,
            query_columns=reference.query_columns,
            llm_description=(
                f"Uploaded table '{reference.logical_name}'. Query as '{QUERY_TABLE}'. "
                f"Columns: {column_pairs}."
            ),
            physical_sql=physical_sql,
        )


_DEFAULT_SERVICE = FileSqlService()


def provision_session_table(
    session_id: str,
    logical_name: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[SqlValue]],
) -> ProvisionedSchema:
    return _DEFAULT_SERVICE.provision_session_table(session_id, logical_name, columns, rows)


def run_scoped_query(session_id: str, logical_name: str, sql: str) -> FileSqlResult:
    return _DEFAULT_SERVICE.run_scoped_query(session_id, logical_name, sql)


def drop_session_tables(session_id: str) -> None:
    _DEFAULT_SERVICE.drop_session_tables(session_id)


def drop_logical_table(session_id: str, logical_name: str) -> None:
    _DEFAULT_SERVICE.drop_logical_table(session_id, logical_name)


def describe_schema_for_llm(session_id: str, logical_name: str) -> ProvisionedSchema:
    return _DEFAULT_SERVICE.describe_schema_for_llm(session_id, logical_name)
