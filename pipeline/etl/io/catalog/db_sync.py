from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import pyarrow.parquet as pq
import pymysql

CATALOG_TABLE_BATCH_LIMIT = 200


@dataclass(frozen=True)
class CatalogColumn:
    name: str
    sql_type: str
    nullable: bool = True


@dataclass(frozen=True)
class CatalogTableSpec:
    parquet_name: str
    table_name: str
    primary_key: str
    columns: tuple[CatalogColumn, ...]


@dataclass(frozen=True)
class CatalogSyncResult:
    table_name: str
    parquet_path: Path
    rows: int
    source_file_versions: tuple[str, ...]
    source_checksum: str
    batch_size: int
    dry_run: bool


CATALOG_ML_MARKET = CatalogTableSpec(
    parquet_name="ml_market",
    table_name="catalog_ml_market",
    primary_key="ml_id",
    columns=(
        CatalogColumn("ml_id", "VARCHAR(32)", nullable=False),
        CatalogColumn("name", "VARCHAR(255)"),
        CatalogColumn("data_source", "VARCHAR(32)"),
        CatalogColumn("atc_codes_json", "LONGTEXT"),
        CatalogColumn("analyze_class", "TINYINT(1)"),
        CatalogColumn("analyze_molecule", "TINYINT(1)"),
        CatalogColumn("analyze_dosage_form", "TINYINT(1)"),
        CatalogColumn("analyze_strength_pack", "TINYINT(1)"),
        CatalogColumn("analyze_nhi_type", "TINYINT(1)"),
        CatalogColumn("analyze_ox_gx", "TINYINT(1)"),
        CatalogColumn("analyze_fish_oil", "TINYINT(1)"),
        CatalogColumn("target_iqvia_1", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_2", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_1", "VARCHAR(255)"),
        CatalogColumn("target_ubist_2", "VARCHAR(255)"),
        CatalogColumn("target_ubist_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_4", "VARCHAR(255)"),
        CatalogColumn("source_file_version", "VARCHAR(512)"),
        CatalogColumn("ingested_at", "DATETIME(6)"),
        CatalogColumn("catalog_manifest_hash", "CHAR(64)"),
    ),
)

CATALOG_CD_MARKET = CatalogTableSpec(
    parquet_name="cd_market",
    table_name="catalog_cd_market",
    primary_key="cd_id",
    columns=(
        CatalogColumn("cd_id", "VARCHAR(32)", nullable=False),
        CatalogColumn("name", "VARCHAR(255)"),
        CatalogColumn("ml_id", "VARCHAR(32)"),
        CatalogColumn("cd_filter_id", "VARCHAR(32)"),
        CatalogColumn("data_source", "VARCHAR(32)"),
        CatalogColumn("analyze_class", "TINYINT(1)"),
        CatalogColumn("analyze_molecule", "TINYINT(1)"),
        CatalogColumn("analyze_dosage_form", "TINYINT(1)"),
        CatalogColumn("analyze_strength_pack", "TINYINT(1)"),
        CatalogColumn("analyze_nhi_type", "TINYINT(1)"),
        CatalogColumn("analyze_ox_gx", "TINYINT(1)"),
        CatalogColumn("analyze_fish_oil", "TINYINT(1)"),
        CatalogColumn("target_iqvia_1", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_2", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_1", "VARCHAR(255)"),
        CatalogColumn("target_ubist_2", "VARCHAR(255)"),
        CatalogColumn("target_ubist_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_4", "VARCHAR(255)"),
        CatalogColumn("source_file_version", "VARCHAR(512)"),
        CatalogColumn("ingested_at", "DATETIME(6)"),
        CatalogColumn("catalog_manifest_hash", "CHAR(64)"),
    ),
)

CATALOG_STRATEGIC_BRAND = CatalogTableSpec(
    parquet_name="strategic_brand",
    table_name="catalog_strategic_brand",
    primary_key="brand_id",
    columns=(
        CatalogColumn("brand_id", "VARCHAR(128)", nullable=False),
        CatalogColumn("name", "VARCHAR(255)"),
        CatalogColumn("merge_name", "VARCHAR(255)"),
        CatalogColumn("ml_id", "VARCHAR(32)"),
        CatalogColumn("cd_id", "VARCHAR(32)"),
        CatalogColumn("is_excluded", "TINYINT(1)"),
        CatalogColumn("is_class_excluded", "TINYINT(1)"),
        CatalogColumn("allowed_atc4_codes_json", "LONGTEXT"),
        CatalogColumn("class", "VARCHAR(255)"),
        CatalogColumn("class_1", "VARCHAR(255)"),
        CatalogColumn("class_2", "VARCHAR(255)"),
        CatalogColumn("molecule", "VARCHAR(255)"),
        CatalogColumn("dosage_form", "VARCHAR(255)"),
        CatalogColumn("strength_pack", "LONGTEXT"),
        CatalogColumn("nhi_type", "VARCHAR(255)"),
        CatalogColumn("ox_gx", "VARCHAR(255)"),
        CatalogColumn("fish_oil", "VARCHAR(255)"),
        CatalogColumn("판매사", "VARCHAR(255)"),
        CatalogColumn("제조사", "VARCHAR(255)"),
        CatalogColumn("source_file_version", "VARCHAR(512)"),
        CatalogColumn("ingested_at", "DATETIME(6)"),
        CatalogColumn("is_jw", "TINYINT(1)"),
        CatalogColumn("is_target", "TINYINT(1)"),
        CatalogColumn("canonical_name", "VARCHAR(255)"),
        CatalogColumn("general_brand_key", "VARCHAR(255)"),
        CatalogColumn("strategy_id", "VARCHAR(32)"),
        CatalogColumn("catalog_manifest_hash", "CHAR(64)"),
    ),
)

CATALOG_TABLES = (
    CATALOG_ML_MARKET,
    CATALOG_CD_MARKET,
    CATALOG_STRATEGIC_BRAND,
)


def sync_catalog_tables(
    conn: pymysql.connections.Connection | None,
    *,
    target_db: str,
    catalog_root: Path,
    batch_size: int = CATALOG_TABLE_BATCH_LIMIT,
    dry_run: bool = False,
) -> tuple[CatalogSyncResult, ...]:
    """Upsert finalized output/catalog parquet files into catalog DB tables."""
    if not dry_run and conn is None:
        raise ValueError("conn is required unless dry_run=True")
    effective_batch_size = _catalog_batch_size(batch_size)
    results: list[CatalogSyncResult] = []
    for spec in CATALOG_TABLES:
        rows, parquet_path, source_checksum = _load_catalog_rows(catalog_root, spec)
        versions = _source_file_versions(rows)
        if not dry_run:
            assert conn is not None
            _create_catalog_table(conn, target_db, spec)
            _upsert_catalog_rows(conn, target_db, spec, rows, effective_batch_size)
        results.append(
            CatalogSyncResult(
                table_name=spec.table_name,
                parquet_path=parquet_path,
                rows=len(rows),
                source_file_versions=versions,
                source_checksum=source_checksum,
                batch_size=effective_batch_size,
                dry_run=dry_run,
            )
        )
    return tuple(results)


def catalog_table_specs() -> tuple[CatalogTableSpec, ...]:
    return CATALOG_TABLES


def quote_id(value: str) -> str:
    if not value or "`" in value or "\x00" in value:
        raise ValueError(f"unsafe SQL identifier: {value}")
    return f"`{value}`"


def _catalog_batch_size(batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return min(batch_size, CATALOG_TABLE_BATCH_LIMIT)


def _catalog_path(catalog_root: Path, parquet_name: str) -> Path:
    return catalog_root / parquet_name / f"{parquet_name}.parquet"


def _load_catalog_rows(catalog_root: Path, spec: CatalogTableSpec) -> tuple[list[dict[str, object]], Path, str]:
    parquet_path = _catalog_path(catalog_root, spec.parquet_name)
    if not parquet_path.exists():
        raise FileNotFoundError(f"catalog parquet not found: {parquet_path}")
    table = pq.read_table(parquet_path)
    raw_rows = table.to_pylist()
    manifest_hash = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    rows = [_row_for_spec(raw_row, spec, manifest_hash) for raw_row in raw_rows]
    return rows, parquet_path, _records_checksum(rows, spec)


def _row_for_spec(raw_row: dict[str, object], spec: CatalogTableSpec, manifest_hash: str) -> dict[str, object]:
    row: dict[str, object] = {}
    for column in spec.columns:
        if column.name == "catalog_manifest_hash":
            row[column.name] = manifest_hash
            continue
        row[column.name] = _db_value(raw_row.get(column.name))
    return row


def _db_value(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _source_file_versions(rows: Sequence[dict[str, object]]) -> tuple[str, ...]:
    versions = {str(row["source_file_version"]) for row in rows if row.get("source_file_version")}
    return tuple(sorted(versions))


def _records_checksum(rows: Sequence[dict[str, object]], spec: CatalogTableSpec) -> str:
    names = tuple(column.name for column in spec.columns)
    payload = [
        {name: _json_value(row.get(name)) for name in names}
        for row in sorted(rows, key=lambda item: str(item.get(spec.primary_key) or ""))
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _create_catalog_table(conn: pymysql.connections.Connection, target_db: str, spec: CatalogTableSpec) -> None:
    column_sql = ",\n  ".join(_column_definition(column) for column in spec.columns)
    sql = (
        f"CREATE TABLE IF NOT EXISTS {quote_id(target_db)}.{quote_id(spec.table_name)} (\n"
        f"  {column_sql},\n"
        f"  PRIMARY KEY ({quote_id(spec.primary_key)})\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql)
    conn.commit()


def _column_definition(column: CatalogColumn) -> str:
    null_sql = "NULL" if column.nullable else "NOT NULL"
    return f"{quote_id(column.name)} {column.sql_type} {null_sql}"


def _upsert_catalog_rows(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
    rows: Sequence[dict[str, object]],
    batch_size: int,
) -> None:
    if not rows:
        return
    names = tuple(column.name for column in spec.columns)
    placeholders = ", ".join(["%s"] * len(names))
    column_sql = ", ".join(quote_id(name) for name in names)
    update_sql = ", ".join(
        f"{quote_id(name)}=VALUES({quote_id(name)})"
        for name in names
        if name != spec.primary_key
    )
    sql = (
        f"INSERT INTO {quote_id(target_db)}.{quote_id(spec.table_name)} ({column_sql}) "
        f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"
    )
    with conn.cursor() as cursor:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            values = [tuple(row.get(name) for name in names) for row in batch]
            cursor.executemany(sql, values)
            conn.commit()
