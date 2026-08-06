from __future__ import annotations

from pathlib import Path

import pymysql

from pipeline.etl.io.catalog import db_sync_rows
from pipeline.etl.io.catalog import db_sync_write
from pipeline.etl.io.catalog.db_sync_rows import (
    export_serving_catalog_tables,
    load_catalog_rows as _load_catalog_rows,
    quote_id,
)
from pipeline.etl.io.catalog.db_sync_references import (
    build_catalog_replacement_reference_report,
)
from pipeline.etl.io.catalog.db_sync_types import (
    CATALOG_TABLE_BATCH_LIMIT,
    CATALOG_TABLES,
    CatalogColumn,
    CatalogParityResult,
    CatalogReplacementApproval,
    CatalogReplacementReferenceReport,
    CatalogSyncResult,
    CatalogTableSpec,
    ServingCatalogExport,
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
    original_loader = db_sync_write.load_catalog_rows
    db_sync_write.load_catalog_rows = _load_catalog_rows
    try:
        return db_sync_write.sync_catalog_tables(
            conn,
            target_db=target_db,
            catalog_root=catalog_root,
            batch_size=batch_size,
            dry_run=dry_run,
            mi_master_sha256=mi_master_sha256,
            replacement=replacement,
            reference_report=reference_report,
        )
    finally:
        db_sync_write.load_catalog_rows = original_loader


def compare_catalog_to_serving(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    catalog_root: Path,
) -> tuple[CatalogParityResult, ...]:
    original_loader = db_sync_rows.load_catalog_rows
    db_sync_rows.load_catalog_rows = _load_catalog_rows
    try:
        return db_sync_rows.compare_catalog_to_serving(
            conn,
            target_db=target_db,
            catalog_root=catalog_root,
        )
    finally:
        db_sync_rows.load_catalog_rows = original_loader


def catalog_table_specs() -> tuple[CatalogTableSpec, ...]:
    return CATALOG_TABLES


__all__ = [
    "CATALOG_TABLE_BATCH_LIMIT",
    "CATALOG_TABLES",
    "CatalogColumn",
    "CatalogParityResult",
    "CatalogReplacementApproval",
    "CatalogReplacementReferenceReport",
    "CatalogSyncResult",
    "CatalogTableSpec",
    "ServingCatalogExport",
    "_load_catalog_rows",
    "build_catalog_replacement_reference_report",
    "catalog_table_specs",
    "compare_catalog_to_serving",
    "export_serving_catalog_tables",
    "quote_id",
    "sync_catalog_tables",
]
