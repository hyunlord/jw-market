from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pymysql

from pipeline.etl.io.catalog.db_sync_rows import quote_id
from pipeline.etl.io.catalog.db_sync_types import CatalogReplacementReferenceReport


@dataclass(frozen=True)
class ReferenceProbe:
    catalog_table: str
    reference_table: str
    reference_column: str
    exact_match: bool = True


REFERENCE_PROBES = (
    ReferenceProbe("catalog_ml_market", "mart_strategic_ml_brand_metric", "ml_id"),
    ReferenceProbe("catalog_ml_market", "mart_strategic_ml_market_metric", "ml_id"),
    ReferenceProbe("catalog_ml_market", "cache_brands", "response_json", False),
    ReferenceProbe("catalog_ml_market", "cache_market_status", "response_json", False),
    ReferenceProbe("catalog_ml_market", "saved_filters", "filter_json", False),
    ReferenceProbe("catalog_cd_market", "mart_strategic_cd_brand_metric", "cd_market_id"),
    ReferenceProbe("catalog_cd_market", "mart_strategic_cd_market_metric", "cd_market_id"),
    ReferenceProbe("catalog_cd_market", "cache_brands", "response_json", False),
    ReferenceProbe("catalog_cd_market", "cache_market_status", "response_json", False),
    ReferenceProbe("catalog_cd_market", "saved_filters", "filter_json", False),
    ReferenceProbe("catalog_strategic_brand", "mart_strategic_ml_brand_metric", "brand_id"),
    ReferenceProbe("catalog_strategic_brand", "mart_strategic_cd_brand_metric", "cd_brand_id"),
    ReferenceProbe("catalog_strategic_brand", "cache_brands", "response_json", False),
    ReferenceProbe("catalog_strategic_brand", "cache_market_status", "response_json", False),
    ReferenceProbe("catalog_strategic_brand", "saved_filters", "filter_json", False),
)


def build_catalog_replacement_reference_report(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    removed_ids_by_table: Mapping[str, tuple[str, ...]],
    inactive_decisions_by_table: Mapping[str, tuple[str, ...]] | None = None,
) -> CatalogReplacementReferenceReport:
    referenced: dict[str, tuple[str, ...]] = {}
    for table, removed_ids in removed_ids_by_table.items():
        table_referenced = [
            removed_id
            for removed_id in removed_ids
            if _id_has_reference(conn, target_db, table, removed_id)
        ]
        if table_referenced:
            referenced[table] = tuple(sorted(table_referenced))
    return CatalogReplacementReferenceReport(
        referenced_ids_by_table=referenced,
        inactive_decisions_by_table=inactive_decisions_by_table or {},
        grounded=True,
    )


def _id_has_reference(
    conn: pymysql.connections.Connection,
    target_db: str,
    catalog_table: str,
    removed_id: str,
) -> bool:
    matched = False
    for probe in REFERENCE_PROBES:
        if probe.catalog_table != catalog_table:
            continue
        if not _reference_column_exists(conn, target_db, probe):
            continue
        if _probe_matches(conn, target_db, probe, removed_id):
            matched = True
    return matched


def _reference_column_exists(
    conn: pymysql.connections.Connection,
    target_db: str,
    probe: ReferenceProbe,
) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 AS `exists` FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s LIMIT 1",
            (target_db, probe.reference_table, probe.reference_column),
        )
        return cursor.fetchone() is not None


def _probe_matches(
    conn: pymysql.connections.Connection,
    target_db: str,
    probe: ReferenceProbe,
    removed_id: str,
) -> bool:
    comparator = "= %s" if probe.exact_match else "LIKE CONCAT('%', %s, '%')"
    sql = (
        f"SELECT 1 FROM {quote_id(target_db)}.{quote_id(probe.reference_table)} "
        f"WHERE {quote_id(probe.reference_column)} {comparator} LIMIT 1"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql, (removed_id,))
        return cursor.fetchone() is not None
