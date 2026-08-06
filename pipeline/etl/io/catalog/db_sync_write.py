from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pymysql

from pipeline.etl.io.catalog.db_sync_rows import load_catalog_rows, quote_id
from pipeline.etl.io.catalog.db_sync_types import (
    CATALOG_TABLES,
    CATALOG_TABLE_BATCH_LIMIT,
    CatalogColumn,
    CatalogReplacementApproval,
    CatalogReplacementReferenceReport,
    CatalogSyncResult,
    CatalogTableSpec,
)


def sync_catalog_tables(
    conn: pymysql.connections.Connection | None,
    *,
    target_db: str,
    catalog_root: Path,
    batch_size: int = CATALOG_TABLE_BATCH_LIMIT,
    dry_run: bool = False,
    mi_master_sha256: str | None = None,
    replacement: CatalogReplacementApproval | None = None,
    reference_report: CatalogReplacementReferenceReport | None = None,
) -> tuple[CatalogSyncResult, ...]:
    if replacement is not None:
        from pipeline.etl.io.catalog.db_sync_replacement import replace_catalog_tables

        return replace_catalog_tables(
            conn,
            target_db=target_db,
            catalog_root=catalog_root,
            batch_size=batch_size,
            dry_run=dry_run,
            mi_master_sha256=mi_master_sha256,
            approval=replacement,
            reference_report=reference_report or CatalogReplacementReferenceReport(),
        )
    return upsert_catalog_tables(
        conn,
        target_db=target_db,
        catalog_root=catalog_root,
        batch_size=batch_size,
        dry_run=dry_run,
        mi_master_sha256=mi_master_sha256,
    )


def upsert_catalog_tables(
    conn: pymysql.connections.Connection | None,
    *,
    target_db: str,
    catalog_root: Path,
    batch_size: int = CATALOG_TABLE_BATCH_LIMIT,
    dry_run: bool = False,
    mi_master_sha256: str | None = None,
) -> tuple[CatalogSyncResult, ...]:
    if not dry_run and conn is None:
        raise ValueError("conn is required unless dry_run=True")
    effective_batch_size = catalog_batch_size(batch_size)
    results: list[CatalogSyncResult] = []
    for spec in CATALOG_TABLES:
        rows, parquet_path, source_checksum = load_catalog_rows(
            catalog_root, spec, mi_master_sha256=mi_master_sha256
        )
        if not dry_run:
            assert conn is not None
            create_catalog_table(conn, target_db, spec)
            upsert_catalog_rows(conn, target_db, spec, rows, effective_batch_size)
        results.append(
            CatalogSyncResult(
                spec.table_name,
                parquet_path,
                len(rows),
                source_file_versions(rows),
                source_checksum,
                single_provenance(rows),
                effective_batch_size,
                dry_run,
            )
        )
    return tuple(results)


def create_catalog_table(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
) -> None:
    column_sql = ",\n  ".join(column_definition(column) for column in spec.columns)
    sql = (
        f"CREATE TABLE IF NOT EXISTS {quote_id(target_db)}.{quote_id(spec.table_name)} (\n"
        f"  {column_sql},\n"
        f"  PRIMARY KEY ({quote_id(spec.primary_key)})\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql)
    conn.commit()


def upsert_catalog_rows(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
    rows: Sequence[dict[str, object]],
    batch_size: int,
    *,
    commit: bool = True,
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
            cursor.executemany(sql, [tuple(row.get(name) for name in names) for row in batch])
            if commit:
                conn.commit()


def catalog_batch_size(batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return min(batch_size, CATALOG_TABLE_BATCH_LIMIT)


def source_file_versions(rows: Sequence[dict[str, object]]) -> tuple[str, ...]:
    versions = {str(row["source_file_version"]) for row in rows if row.get("source_file_version")}
    return tuple(sorted(versions))


def single_provenance(rows: Sequence[dict[str, object]]) -> str | None:
    hashes = {str(row["mi_master_sha256"]) for row in rows if row.get("mi_master_sha256")}
    if len(hashes) > 1:
        raise RuntimeError(f"catalog rows contain multiple MI Master hashes: {sorted(hashes)}")
    return next(iter(hashes), None)


def column_definition(column: CatalogColumn) -> str:
    null_sql = "NULL" if column.nullable else "NOT NULL"
    return f"{quote_id(column.name)} {column.sql_type} {null_sql}"
