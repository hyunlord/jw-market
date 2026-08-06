from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import pyarrow.parquet as pq
import pyarrow as pa
import pymysql

from pipeline.etl.io.catalog.paths import catalog_file

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
    mi_master_sha256: str | None
    batch_size: int
    dry_run: bool


@dataclass(frozen=True)
class CatalogParityResult:
    parquet_name: str
    table_name: str
    candidate_rows: int
    serving_rows: int
    missing_primary_keys: tuple[str, ...]
    added_primary_keys: tuple[str, ...]
    changed_primary_keys: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not (
            self.missing_primary_keys
            or self.added_primary_keys
            or self.changed_primary_keys
        )


@dataclass(frozen=True)
class ServingCatalogExport:
    parquet_name: str
    table_name: str
    rows: int
    source_file_versions: tuple[str, ...]
    manifest_hash: str
    mi_master_sha256: str | None


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
        CatalogColumn("mi_master_sha256", "CHAR(64)"),
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
        CatalogColumn("mi_master_sha256", "CHAR(64)"),
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
        CatalogColumn("mi_master_sha256", "CHAR(64)"),
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
    mi_master_sha256: str | None = None,
) -> tuple[CatalogSyncResult, ...]:
    """Upsert finalized output/catalog parquet files into catalog DB tables."""
    if not dry_run and conn is None:
        raise ValueError("conn is required unless dry_run=True")
    effective_batch_size = _catalog_batch_size(batch_size)
    results: list[CatalogSyncResult] = []
    for spec in CATALOG_TABLES:
        rows, parquet_path, source_checksum = _load_catalog_rows(
            catalog_root,
            spec,
            mi_master_sha256=mi_master_sha256,
        )
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
                mi_master_sha256=_single_provenance(rows),
                batch_size=effective_batch_size,
                dry_run=dry_run,
            )
        )
    return tuple(results)


def catalog_table_specs() -> tuple[CatalogTableSpec, ...]:
    return CATALOG_TABLES


def export_serving_catalog_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    catalog_root: Path,
) -> tuple[ServingCatalogExport, ...]:
    """Export the DB-backed catalog core using candidate parquet schemas."""

    exports: list[ServingCatalogExport] = []
    for spec in CATALOG_TABLES:
        parquet_path = _catalog_path(catalog_root, spec.parquet_name)
        template = pq.read_table(parquet_path)
        names = tuple(template.schema.names)
        if spec.primary_key not in names:
            raise ValueError(
                f"{spec.parquet_name} template is missing primary key {spec.primary_key}"
            )
        projection = ", ".join(quote_id(name) for name in names)
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {projection} FROM {quote_id(target_db)}."
                f"{quote_id(spec.table_name)} ORDER BY {quote_id(spec.primary_key)}"
            )
            rows = list(cursor.fetchall())
            cursor.execute(
                f"SELECT DISTINCT {quote_id('source_file_version')} AS value "
                f"FROM {quote_id(target_db)}.{quote_id(spec.table_name)} "
                f"ORDER BY {quote_id('source_file_version')}"
            )
            versions = tuple(str(row["value"]) for row in cursor.fetchall())
            cursor.execute(
                f"SELECT DISTINCT {quote_id('catalog_manifest_hash')} AS value "
                f"FROM {quote_id(target_db)}.{quote_id(spec.table_name)} "
                f"ORDER BY {quote_id('catalog_manifest_hash')}"
            )
            hashes = tuple(str(row["value"]) for row in cursor.fetchall())
            cursor.execute(
                f"SELECT DISTINCT {quote_id('mi_master_sha256')} AS value "
                f"FROM {quote_id(target_db)}.{quote_id(spec.table_name)} "
                f"WHERE {quote_id('mi_master_sha256')} IS NOT NULL "
                f"ORDER BY {quote_id('mi_master_sha256')}"
            )
            mi_master_hashes = tuple(str(row["value"]) for row in cursor.fetchall())
        if len(hashes) != 1 or len(hashes[0]) != 64:
            raise RuntimeError(
                f"{spec.table_name} serving manifest hash is not singular: {hashes}"
            )
        if len(mi_master_hashes) > 1:
            raise RuntimeError(
                f"{spec.table_name} MI Master hash is not singular: {mi_master_hashes}"
            )
        bool_fields = {
            field.name for field in template.schema if pa.types.is_boolean(field.type)
        }
        normalized_rows = [
            {
                key: bool(value) if key in bool_fields and value is not None else value
                for key, value in row.items()
            }
            for row in rows
        ]
        anchored = pa.Table.from_pylist(normalized_rows, schema=template.schema)
        pq.write_table(anchored, parquet_path)
        exports.append(
            ServingCatalogExport(
                parquet_name=spec.parquet_name,
                table_name=spec.table_name,
                rows=anchored.num_rows,
                source_file_versions=versions,
                manifest_hash=hashes[0],
                mi_master_sha256=mi_master_hashes[0] if mi_master_hashes else None,
            )
        )
    return tuple(exports)


def compare_catalog_to_serving(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    catalog_root: Path,
) -> tuple[CatalogParityResult, ...]:
    """Compare catalog business columns to serving tables with deterministic PK ordering."""

    results: list[CatalogParityResult] = []
    for spec in CATALOG_TABLES:
        candidate_rows, _, _ = _load_catalog_rows(catalog_root, spec)
        compare_columns = tuple(
            column.name
            for column in spec.columns
            if column.name not in {"ingested_at", "catalog_manifest_hash"}
        )
        projection = ", ".join(quote_id(name) for name in compare_columns)
        sql = (
            f"SELECT {projection} FROM {quote_id(target_db)}.{quote_id(spec.table_name)} "
            f"ORDER BY {quote_id(spec.primary_key)}"
        )
        with conn.cursor() as cursor:
            cursor.execute(sql)
            serving_rows = list(cursor.fetchall())
        candidate_by_key = {
            str(row.get(spec.primary_key) or ""): _business_row(row, compare_columns)
            for row in candidate_rows
        }
        serving_by_key = {
            str(row.get(spec.primary_key) or ""): _business_row(row, compare_columns)
            for row in serving_rows
        }
        candidate_keys = set(candidate_by_key)
        serving_keys = set(serving_by_key)
        shared = candidate_keys & serving_keys
        results.append(
            CatalogParityResult(
                parquet_name=spec.parquet_name,
                table_name=spec.table_name,
                candidate_rows=len(candidate_rows),
                serving_rows=len(serving_rows),
                missing_primary_keys=tuple(sorted(serving_keys - candidate_keys)),
                added_primary_keys=tuple(sorted(candidate_keys - serving_keys)),
                changed_primary_keys=tuple(
                    sorted(
                        key
                        for key in shared
                        if candidate_by_key[key] != serving_by_key[key]
                    )
                ),
            )
        )
    return tuple(results)


def quote_id(value: str) -> str:
    if not value or "`" in value or "\x00" in value:
        raise ValueError(f"unsafe SQL identifier: {value}")
    return f"`{value}`"


def _catalog_batch_size(batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return min(batch_size, CATALOG_TABLE_BATCH_LIMIT)


def _catalog_path(catalog_root: Path, parquet_name: str) -> Path:
    return catalog_file(catalog_root, parquet_name)


def _load_catalog_rows(
    catalog_root: Path,
    spec: CatalogTableSpec,
    *,
    mi_master_sha256: str | None = None,
) -> tuple[list[dict[str, object]], Path, str]:
    parquet_path = _catalog_path(catalog_root, spec.parquet_name)
    if not parquet_path.exists():
        raise FileNotFoundError(f"catalog parquet not found: {parquet_path}")
    table = pq.read_table(parquet_path)
    raw_rows = table.to_pylist()
    manifest_hash = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    rows = [
        _row_for_spec(raw_row, spec, manifest_hash, mi_master_sha256)
        for raw_row in raw_rows
    ]
    return rows, parquet_path, _records_checksum(rows, spec)


def _row_for_spec(
    raw_row: dict[str, object],
    spec: CatalogTableSpec,
    manifest_hash: str,
    mi_master_sha256: str | None,
) -> dict[str, object]:
    row: dict[str, object] = {}
    for column in spec.columns:
        if column.name == "catalog_manifest_hash":
            row[column.name] = manifest_hash
            continue
        if column.name == "mi_master_sha256" and column.name not in raw_row:
            row[column.name] = mi_master_sha256
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


def _single_provenance(rows: Sequence[dict[str, object]]) -> str | None:
    hashes = {
        str(row["mi_master_sha256"])
        for row in rows
        if row.get("mi_master_sha256")
    }
    if len(hashes) > 1:
        raise RuntimeError(f"catalog rows contain multiple MI Master hashes: {sorted(hashes)}")
    return next(iter(hashes), None)


def _records_checksum(rows: Sequence[dict[str, object]], spec: CatalogTableSpec) -> str:
    names = tuple(column.name for column in spec.columns)
    payload = [
        {name: _json_value(row.get(name)) for name in names}
        for row in sorted(rows, key=lambda item: str(item.get(spec.primary_key) or ""))
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _business_row(
    row: dict[str, object],
    columns: Sequence[str],
) -> tuple[object, ...]:
    return tuple(_json_value(row.get(column)) for column in columns)


def _create_catalog_table(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
) -> None:
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
