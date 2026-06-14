from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.brand import strategic_product as strategic_product
from pipeline.etl.io.catalog._lib.common import read_parquet_rows
from pipeline.etl.io.catalog._lib.catalog_parquet import write_typed_parquet


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_cd_product_records(
    strategic_product_path: Path,
    cd_brand_path: Path,
    cd_market_path: Path,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    product_rows = read_parquet_rows(strategic_product_path)
    cd_brand_rows = read_parquet_rows(cd_brand_path)
    cd_market_rows = read_parquet_rows(cd_market_path)
    cd_brand_by_id = {str(row["brand_id"]): row for row in cd_brand_rows}
    cd_ids = {str(row["cd_id"]) for row in cd_market_rows}
    timestamp = ingested_at or utc_now_datetime()
    cd_product_rows: list[dict[str, Any]] = []
    dropped_non_null_cd_rows: list[dict[str, Any]] = []
    cd_mismatch_rows: list[dict[str, Any]] = []
    for row in product_rows:
        if row.get("cd_id") is None:
            continue
        brand_id = str(row["brand_id"])
        cd_brand = cd_brand_by_id.get(brand_id)
        if cd_brand is None:
            dropped_non_null_cd_rows.append({"product_id": row.get("product_id"), "brand_id": brand_id, "cd_id": row.get("cd_id")})
            continue
        if row.get("cd_id") != cd_brand.get("cd_id"):
            cd_mismatch_rows.append({"product_id": row.get("product_id"), "brand_id": brand_id, "product_cd_id": row.get("cd_id"), "brand_cd_id": cd_brand.get("cd_id")})
            continue
        out = {column: row.get(column) for column in strategic_product.EXPECTED_COLUMNS}
        out["ingested_at"] = timestamp
        cd_product_rows.append(out)
    if dropped_non_null_cd_rows:
        raise ValueError(f"cd_product brand FK missing from cd_brand: {dropped_non_null_cd_rows[:10]}")
    if cd_mismatch_rows:
        raise ValueError(f"cd_product.cd_id vs cd_brand.cd_id mismatch: {cd_mismatch_rows[:10]}")
    validate_records(cd_product_rows, cd_brand_rows, cd_ids)
    return cd_product_rows


def validate_records(records: list[dict[str, Any]], cd_brand_rows: list[dict[str, Any]], cd_ids: set[str]) -> None:
    expected_columns = tuple(strategic_product.EXPECTED_COLUMNS)
    product_ids = [str(row["product_id"]) for row in records]
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("cd_product.product_id must be unique")
    cd_brand_ids = {str(row["brand_id"]) for row in cd_brand_rows}
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != expected_columns:
            raise ValueError(f"row {index} columns mismatch: expected={expected_columns}, actual={tuple(record.keys())}")
        if record["cd_id"] is None:
            raise ValueError(f"row {index} cd_id must be non-null")
        if str(record["cd_id"]) not in cd_ids:
            raise ValueError(f"row {index} missing cd_market FK: {record['cd_id']}")
        if str(record["brand_id"]) not in cd_brand_ids:
            raise ValueError(f"row {index} missing cd_brand FK: {record['brand_id']}")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"row {index} ingested_at must be datetime")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_typed_parquet(records, output_file, strategic_product.STRATEGIC_PRODUCT_SCHEMA)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != strategic_product.STRATEGIC_PRODUCT_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={strategic_product.STRATEGIC_PRODUCT_SCHEMA}\nactual={table.schema}")
