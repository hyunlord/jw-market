from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq
import pymysql

from pipeline.etl.io.catalog.db_sync_types import (
    CATALOG_TABLES,
    CatalogParityResult,
    CatalogTableSpec,
    ServingCatalogExport,
)
from pipeline.etl.io.catalog.paths import catalog_file


def export_serving_catalog_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    catalog_root: Path,
) -> tuple[ServingCatalogExport, ...]:
    exports: list[ServingCatalogExport] = []
    for spec in CATALOG_TABLES:
        parquet_path = catalog_file(catalog_root, spec.parquet_name)
        template = pq.read_table(parquet_path)
        names = tuple(template.schema.names)
        if spec.primary_key not in names:
            raise ValueError(f"{spec.parquet_name} template is missing primary key")
        rows, versions, hashes, mi_hashes = _serving_export_rows(
            conn, target_db, spec, names
        )
        if len(hashes) != 1 or len(hashes[0]) != 64:
            raise RuntimeError(f"{spec.table_name} serving manifest hash is not singular")
        if len(mi_hashes) > 1:
            raise RuntimeError(f"{spec.table_name} MI Master hash is not singular")
        bool_fields = {
            field.name for field in template.schema if pa.types.is_boolean(field.type)
        }
        normalized = [
            {
                key: bool(value) if key in bool_fields and value is not None else value
                for key, value in row.items()
            }
            for row in rows
        ]
        anchored = pa.Table.from_pylist(normalized, schema=template.schema)
        pq.write_table(anchored, parquet_path)
        exports.append(
            ServingCatalogExport(
                spec.parquet_name,
                spec.table_name,
                anchored.num_rows,
                versions,
                hashes[0],
                mi_hashes[0] if mi_hashes else None,
            )
        )
    return tuple(exports)


def compare_catalog_to_serving(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    catalog_root: Path,
) -> tuple[CatalogParityResult, ...]:
    results: list[CatalogParityResult] = []
    for spec in CATALOG_TABLES:
        candidate_rows, _, _ = load_catalog_rows(catalog_root, spec)
        serving_rows = serving_business_rows(conn, target_db, spec)
        compare_columns = business_columns(spec)
        candidate_by_key = row_map(candidate_rows, spec, compare_columns)
        serving_by_key = row_map(serving_rows, spec, compare_columns)
        candidate_keys = set(candidate_by_key)
        serving_keys = set(serving_by_key)
        shared = candidate_keys & serving_keys
        results.append(
            CatalogParityResult(
                spec.parquet_name,
                spec.table_name,
                len(candidate_rows),
                len(serving_rows),
                tuple(sorted(serving_keys - candidate_keys)),
                tuple(sorted(candidate_keys - serving_keys)),
                tuple(sorted(k for k in shared if candidate_by_key[k] != serving_by_key[k])),
            )
        )
    return tuple(results)


def load_catalog_rows(
    catalog_root: Path,
    spec: CatalogTableSpec,
    *,
    mi_master_sha256: str | None = None,
) -> tuple[list[dict[str, object]], Path, str]:
    parquet_path = catalog_file(catalog_root, spec.parquet_name)
    if not parquet_path.exists():
        raise FileNotFoundError(f"catalog parquet not found: {parquet_path}")
    table = pq.read_table(parquet_path)
    manifest_hash = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    rows = [
        _row_for_spec(raw, spec, manifest_hash, mi_master_sha256)
        for raw in table.to_pylist()
    ]
    return rows, parquet_path, records_checksum(rows, spec)


def serving_business_rows(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
) -> list[dict[str, object]]:
    compare_columns = business_columns(spec)
    projection = ", ".join(quote_id(name) for name in compare_columns)
    sql = (
        f"SELECT {projection} FROM {quote_id(target_db)}.{quote_id(spec.table_name)} "
        f"ORDER BY {quote_id(spec.primary_key)}"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def business_columns(spec: CatalogTableSpec) -> tuple[str, ...]:
    return tuple(
        column.name
        for column in spec.columns
        if column.name not in {"ingested_at", "catalog_manifest_hash"}
    )


def row_map(
    rows: Sequence[dict[str, object]],
    spec: CatalogTableSpec,
    columns: Sequence[str],
) -> dict[str, tuple[object, ...]]:
    return {
        str(row.get(spec.primary_key) or ""): tuple(_json_value(row.get(c)) for c in columns)
        for row in rows
    }


def quote_id(value: str) -> str:
    if not value or "`" in value or "\x00" in value:
        raise ValueError(f"unsafe SQL identifier: {value}")
    return f"`{value}`"


def records_checksum(rows: Sequence[dict[str, object]], spec: CatalogTableSpec) -> str:
    names = tuple(column.name for column in spec.columns)
    payload = [
        {name: _json_value(row.get(name)) for name in names}
        for row in sorted(rows, key=lambda item: str(item.get(spec.primary_key) or ""))
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _serving_export_rows(
    conn: pymysql.connections.Connection,
    target_db: str,
    spec: CatalogTableSpec,
    names: Sequence[str],
) -> tuple[list[dict[str, object]], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    projection = ", ".join(quote_id(name) for name in names)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {projection} FROM {quote_id(target_db)}."
            f"{quote_id(spec.table_name)} ORDER BY {quote_id(spec.primary_key)}"
        )
        rows = list(cursor.fetchall())
        versions = _distinct(cursor, target_db, spec, "source_file_version")
        hashes = _distinct(cursor, target_db, spec, "catalog_manifest_hash")
        mi_hashes = _distinct(cursor, target_db, spec, "mi_master_sha256", not_null=True)
    return rows, versions, hashes, mi_hashes


def _distinct(
    cursor: object,
    target_db: str,
    spec: CatalogTableSpec,
    column: str,
    *,
    not_null: bool = False,
) -> tuple[str, ...]:
    where = f" WHERE {quote_id(column)} IS NOT NULL" if not_null else ""
    cursor.execute(
        f"SELECT DISTINCT {quote_id(column)} AS value "
        f"FROM {quote_id(target_db)}.{quote_id(spec.table_name)}{where} "
        f"ORDER BY {quote_id(column)}"
    )
    return tuple(str(row["value"]) for row in cursor.fetchall())


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
        elif column.name == "mi_master_sha256" and column.name not in raw_row:
            row[column.name] = mi_master_sha256
        else:
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


def _json_value(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value
