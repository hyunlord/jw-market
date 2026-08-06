from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pymysql

from pipeline.etl.io.catalog.db_sync_rows import (
    business_columns,
    load_catalog_rows,
    quote_id,
    row_map,
    serving_business_rows,
)
from pipeline.etl.io.catalog.db_sync_types import (
    CATALOG_TABLES,
    CatalogReplacementApproval,
    CatalogReplacementReferenceReport,
    CatalogSyncResult,
    CatalogTableSpec,
)
from pipeline.etl.io.catalog.db_sync_write import (
    catalog_batch_size,
    single_provenance,
    source_file_versions,
    upsert_catalog_rows,
)


@dataclass(frozen=True)
class CandidateTable:
    spec: CatalogTableSpec
    rows: list[dict[str, object]]
    parquet_path: Path
    checksum: str
    removed_ids: tuple[str, ...]


def replace_catalog_tables(
    conn: pymysql.connections.Connection | None,
    *,
    target_db: str,
    catalog_root: Path,
    batch_size: int,
    dry_run: bool,
    mi_master_sha256: str | None,
    approval: CatalogReplacementApproval,
    reference_report: CatalogReplacementReferenceReport,
) -> tuple[CatalogSyncResult, ...]:
    if not dry_run and conn is None:
        raise ValueError("conn is required unless dry_run=True")
    assert conn is not None
    effective_batch_size = catalog_batch_size(batch_size)
    candidates = _load_candidates(
        conn,
        target_db,
        catalog_root,
        mi_master_sha256,
    )
    _validate_approval(candidates, approval, reference_report)
    if dry_run:
        return _results(candidates, effective_batch_size, True)
    try:
        _begin_transaction(conn)
        _delete_removed(conn, target_db, candidates)
        for candidate in candidates:
            upsert_catalog_rows(
                conn,
                target_db,
                candidate.spec,
                candidate.rows,
                effective_batch_size,
                commit=False,
        )
        mismatches = _parity_mismatches(conn, target_db, candidates)
        if mismatches:
            raise RuntimeError(
                "catalog replacement parity failed: " + "; ".join(mismatches)
            )
        conn.commit()
    except (RuntimeError, ValueError, pymysql.MySQLError):
        conn.rollback()
        raise
    return _results(candidates, effective_batch_size, False)


def _begin_transaction(conn: pymysql.connections.Connection) -> None:
    begin = getattr(conn, "begin", None)
    if callable(begin):
        begin()
        return
    with conn.cursor() as cursor:
        cursor.execute("START TRANSACTION")


def _load_candidates(
    conn: pymysql.connections.Connection,
    target_db: str,
    catalog_root: Path,
    mi_master_sha256: str | None,
) -> tuple[CandidateTable, ...]:
    candidates: list[CandidateTable] = []
    for spec in CATALOG_TABLES:
        rows, parquet_path, checksum = load_catalog_rows(
            catalog_root, spec, mi_master_sha256=mi_master_sha256
        )
        candidate_ids = {str(row.get(spec.primary_key) or "") for row in rows}
        current_ids = set(_current_ids(conn, target_db, spec))
        candidates.append(
            CandidateTable(
                spec,
                rows,
                parquet_path,
                checksum,
                tuple(sorted(current_ids - candidate_ids)),
            )
        )
    return tuple(candidates)


def _validate_approval(
    candidates: Sequence[CandidateTable],
    approval: CatalogReplacementApproval,
    report: CatalogReplacementReferenceReport,
) -> None:
    expected = {
        candidate.spec.table_name: candidate.removed_ids
        for candidate in candidates
        if candidate.removed_ids
    }
    actual = {
        table: tuple(sorted(ids))
        for table, ids in approval.removed_ids_by_table.items()
        if ids
    }
    if actual != expected:
        raise ValueError("removed catalog IDs require exact approval")
    blocked = []
    for table, removed_ids in expected.items():
        referenced = set(report.referenced_ids_by_table.get(table, ()))
        inactive = set(report.inactive_decisions_by_table.get(table, ()))
        blocked.extend(
            f"{table}:{removed_id}"
            for removed_id in removed_ids
            if removed_id in referenced and removed_id not in inactive
        )
    if blocked:
        raise ValueError("referenced catalog removals require inactive decision")


def _current_ids(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
) -> tuple[str, ...]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {quote_id(spec.primary_key)} FROM "
            f"{quote_id(target_db)}.{quote_id(spec.table_name)} "
            f"ORDER BY {quote_id(spec.primary_key)}"
        )
        return tuple(str(row[spec.primary_key]) for row in cursor.fetchall())


def _delete_removed(
    conn: pymysql.connections.Connection,
    target_db: str,
    candidates: Sequence[CandidateTable],
) -> None:
    with conn.cursor() as cursor:
        for candidate in candidates:
            if not candidate.removed_ids:
                continue
            placeholders = ", ".join(["%s"] * len(candidate.removed_ids))
            cursor.execute(
                f"DELETE FROM {quote_id(target_db)}."
                f"{quote_id(candidate.spec.table_name)} "
                f"WHERE {quote_id(candidate.spec.primary_key)} IN ({placeholders})",
                candidate.removed_ids,
            )


def _parity_mismatches(
    conn: pymysql.connections.Connection,
    target_db: str,
    candidates: Sequence[CandidateTable],
) -> list[str]:
    mismatches: list[str] = []
    for candidate in candidates:
        columns = business_columns(candidate.spec)
        candidate_map = row_map(candidate.rows, candidate.spec, columns)
        serving_map = row_map(
            serving_business_rows(conn, target_db, candidate.spec),
            candidate.spec,
            columns,
        )
        if candidate_map != serving_map:
            mismatches.append(candidate.spec.table_name)
    return mismatches


def _results(
    candidates: Sequence[CandidateTable],
    batch_size: int,
    dry_run: bool,
) -> tuple[CatalogSyncResult, ...]:
    return tuple(
        CatalogSyncResult(
            candidate.spec.table_name,
            candidate.parquet_path,
            len(candidate.rows),
            source_file_versions(candidate.rows),
            candidate.checksum,
            single_provenance(candidate.rows),
            batch_size,
            dry_run,
        )
        for candidate in candidates
    )
