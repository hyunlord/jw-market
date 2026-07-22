"""Load MI Master market-group source tables into the local brand-activity stage DB."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar

import pymysql

from pipeline.etl.io.catalog.master.mapping_table import load_mapping_records
from pipeline.etl.io.catalog.master.mapping_table_schema import (
    DEFAULT_CATALOG_PATH,
    EXPECTED_ROW_COUNT as EXPECTED_MAPPING_ROW_COUNT,
    MASTER_MAPPING_TABLE_COLUMNS,
)
from pipeline.etl.io.catalog.master.mapping_table_validation import validate_records as validate_mapping_records
from pipeline.etl.io.catalog.master.market_definition import iter_market_definition_rows
from pipeline.etl.io.catalog.master.market_definition_schema import (
    EXPECTED_ROW_COUNT as EXPECTED_MARKET_DEFINITION_ROW_COUNT,
    MASTER_MARKET_DEFINITION_COLUMNS,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import connect_mariadb, read_env_file


T = TypeVar("T")

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
SCHEMA: Final = os.environ.get("MARKET_GROUP_SCHEMA", "jw_brand_activity_stage")
MARKET_DEFINITION_TABLE: Final = "stg_master_market_definition"
MAPPING_TABLE: Final = "stg_master_mapping_table"
ALLOWED_SCHEMAS: Final = frozenset({"jw_brand_activity_stage"})
ISOLATED_SCHEMA_PATTERN: Final = re.compile(r"^jw_ingest_[A-Za-z0-9_]+$")
BATCH_SIZE: Final = 200
DB_TEXT_COLUMNS: Final = {
    "market_atc_codes_json",
    "full_market_atc4_codes_json",
    "direct_competition_brands_json",
    "description",
    "analysis_levels_json",
    "analysis_level_etc",
    "target_customer_priority_json",
    "raw_row_json",
}
CONFIG_CATALOG_PATH: Final = REPO_ROOT / "pipeline/etl/config/master_column_mapping_catalog.md"


@dataclass(frozen=True, slots=True)
class LoadSummary:
    """Row-count evidence from one MI Master DB load."""

    schema: str
    market_definition: int
    mapping: int
    saved: bool


class MarketGroupLoadError(RuntimeError):
    """Raised when MI Master market-group loading would violate the contract."""


def load(xlsx_path: Path, *, schema: str = SCHEMA, save: bool = True, ingested_at: str | None = None) -> LoadSummary:
    """Build MI Master records and optionally replace the isolated stage tables."""
    safe_schema = _validated_schema(schema)
    stable_ingested_at = ingested_at or _source_ingested_at(xlsx_path)
    market_definition_rows = list(iter_market_definition_rows(xlsx_path, ingested_at=stable_ingested_at))
    mapping_records, mapping_stats = load_mapping_records(
        xlsx_path,
        _resolve_catalog_path(),
        ingested_at=stable_ingested_at,
    )
    validate_mapping_records(mapping_records, mapping_stats)
    if len(market_definition_rows) != EXPECTED_MARKET_DEFINITION_ROW_COUNT:
        raise MarketGroupLoadError(
            f"market_definition row count mismatch: expected {EXPECTED_MARKET_DEFINITION_ROW_COUNT}, "
            f"got {len(market_definition_rows)}"
        )
    if len(mapping_records) != EXPECTED_MAPPING_ROW_COUNT:
        raise MarketGroupLoadError(f"mapping row count mismatch: expected {EXPECTED_MAPPING_ROW_COUNT}, got {len(mapping_records)}")
    if save:
        _replace_tables(safe_schema, market_definition_rows, mapping_records)
    return LoadSummary(
        schema=safe_schema,
        market_definition=len(market_definition_rows),
        mapping=len(mapping_records),
        saved=save,
    )


def _replace_tables(schema: str, market_definition_rows: list[dict[str, Any]], mapping_records: list[dict[str, Any]]) -> None:
    """Create and replace only the two MI Master stage tables."""
    connection = connect_mariadb(read_env_file())
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION")
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{schema}`")
            cursor.execute(_create_table_sql(schema, MARKET_DEFINITION_TABLE, MASTER_MARKET_DEFINITION_COLUMNS))
            cursor.execute(_create_table_sql(schema, MAPPING_TABLE, MASTER_MAPPING_TABLE_COLUMNS))
            cursor.execute(f"TRUNCATE TABLE `{schema}`.`{MARKET_DEFINITION_TABLE}`")
            cursor.execute(f"TRUNCATE TABLE `{schema}`.`{MAPPING_TABLE}`")
            _insert_rows(cursor, schema, MARKET_DEFINITION_TABLE, MASTER_MARKET_DEFINITION_COLUMNS, market_definition_rows)
            _insert_rows(cursor, schema, MAPPING_TABLE, MASTER_MAPPING_TABLE_COLUMNS, mapping_records)
            cursor.execute("COMMIT")
    except pymysql.MySQLError:
        with connection.cursor() as cursor:
            cursor.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _create_table_sql(schema: str, table: str, columns: Sequence[str]) -> str:
    """Return a strict staging-table DDL with deterministic column order."""
    definitions = ",\n            ".join(f"`{column}` {_column_type(column)}" for column in columns)
    return f"""
        CREATE TABLE IF NOT EXISTS `{schema}`.`{table}` (
            {definitions}
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


def _column_type(column: str) -> str:
    """Choose a conservative MariaDB type for MI Master scalar and JSON fields."""
    if column == "mapping_id":
        return "VARCHAR(96) NOT NULL PRIMARY KEY"
    if column == "strategic_market_id":
        return "VARCHAR(32) NOT NULL"
    if column == "ingested_at":
        return "VARCHAR(64) NOT NULL"
    if column in DB_TEXT_COLUMNS:
        return "LONGTEXT NULL"
    return "VARCHAR(255) NULL"


def _insert_rows(
    cursor: pymysql.cursors.Cursor,
    schema: str,
    table: str,
    columns: Sequence[str],
    rows: list[dict[str, Any]],
) -> None:
    """Insert rows in bounded batches to keep Galera-safe behavior portable."""
    if not rows:
        return
    quoted = ", ".join(f"`{column}`" for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO `{schema}`.`{table}` ({quoted}) VALUES ({placeholders})"
    for batch in _chunks(rows, BATCH_SIZE):
        cursor.executemany(sql, [_row_tuple(row, columns) for row in batch])


def _row_tuple(row: dict[str, Any], columns: Sequence[str]) -> tuple[Any, ...]:
    """Serialize nested values before handing them to pymysql."""
    return tuple(_db_value(row.get(column)) for column in columns)


def _db_value(value: Any) -> Any:
    """Return a DB-compatible scalar while preserving existing JSON text fields."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _chunks(items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    """Yield fixed-size batches."""
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _validated_schema(schema: str) -> str:
    """Refuse accidental writes outside the isolated brand-activity stage schema."""
    if schema not in ALLOWED_SCHEMAS and ISOLATED_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise MarketGroupLoadError(
            f"refusing schema {schema!r}; allowed={sorted(ALLOWED_SCHEMAS)} or jw_ingest_*"
        )
    return schema


def _resolve_xlsx(path_pattern: str) -> Path:
    """Resolve a literal or globbed MI Master workbook path."""
    path = Path(path_pattern)
    if path.exists():
        return path
    matches = sorted(Path(match) for match in glob.glob(path_pattern) if Path(match).is_file())
    if not matches:
        raise FileNotFoundError(f"MI Master workbook not found: {path_pattern}")
    return matches[0]


def _resolve_catalog_path() -> Path:
    """Resolve the checked-in mapping catalog despite legacy default path drift."""
    if DEFAULT_CATALOG_PATH.exists():
        return DEFAULT_CATALOG_PATH
    if CONFIG_CATALOG_PATH.exists():
        return CONFIG_CATALOG_PATH
    raise FileNotFoundError(f"master column mapping catalog not found: {DEFAULT_CATALOG_PATH} or {CONFIG_CATALOG_PATH}")


def _source_ingested_at(xlsx_path: Path) -> str:
    """Return a deterministic load marker for reproducible replay runs."""
    digest = hashlib.sha256()
    with xlsx_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"source_sha256:{digest.hexdigest()[:16]}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load MI Master market-group tables into jw_brand_activity_stage.")
    parser.add_argument("--xlsx", required=True, help="MI Master workbook path or glob.")
    parser.add_argument("--schema", default=SCHEMA)
    parser.add_argument(
        "--ingested-at",
        help="Optional explicit ingested_at marker. Defaults to a deterministic source workbook hash.",
    )
    parser.add_argument("--no-save", action="store_true", help="Build and validate records without writing DB tables.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    summary = load(_resolve_xlsx(args.xlsx), schema=args.schema, save=not args.no_save, ingested_at=args.ingested_at)
    print(
        json.dumps(
            {
                "schema": summary.schema,
                "stg_master_market_definition": summary.market_definition,
                "stg_master_mapping_table": summary.mapping,
                "saved": summary.saved,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
