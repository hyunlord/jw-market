"""Typed values crossing the file-SQL service boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

SqlValue: TypeAlias = str | int | float | bytes | None


@dataclass(frozen=True, slots=True)
class TableReference:
    session_hash: str
    logical_name: str
    database_path: Path
    physical_table: str
    source_columns: tuple[str, ...]
    query_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProvisionedSchema:
    logical_name: str
    query_table: str
    source_columns: tuple[str, ...]
    query_columns: tuple[str, ...]
    llm_description: str
    physical_sql: str


@dataclass(frozen=True, slots=True)
class FileSqlResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[SqlValue, ...], ...]


@dataclass(frozen=True, slots=True)
class FileSqlInventory:
    database_names: tuple[str, ...]
    table_names: tuple[str, ...]
    connection_uri: str
