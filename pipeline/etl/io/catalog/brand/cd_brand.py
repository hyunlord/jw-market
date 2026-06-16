from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pipeline.etl.io.catalog._lib.common import read_parquet_rows
from pipeline.etl.io.catalog._lib.catalog_parquet import write_typed_parquet
from pipeline.etl.io.catalog._lib.expected_counts import expected_int
from pipeline.etl.io.catalog.brand import strategic_brand, strategic_product

EXPECTED_ROW_COUNT = expected_int("cd_brand.row_count")


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def recompute_cd_assignments(
    brand_rows: list[dict[str, Any]],
    cd_market_rows: list[dict[str, Any]],
    cd_filter_rows: list[dict[str, Any]],
    contexts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    filter_by_id = {str(row["cd_filter_id"]): row for row in cd_filter_rows}
    cd_markets_for_ml: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cd_market_rows:
        cd_markets_for_ml[str(row["ml_id"])].append(row)

    mismatches: list[dict[str, Any]] = []
    for row in brand_rows:
        brand_id = str(row["brand_id"])
        context = contexts.get(brand_id)
        if context is None:
            mismatches.append({"brand_id": brand_id, "actual_cd_id": row.get("cd_id"), "recomputed_cd_id": None, "candidates": "", "reason": "missing_source_context"})
            continue
        match_context = {
            "ml_id": row["ml_id"],
            "atc4_code": context.get("atc4_code"),
            "class": row.get("class"),
            "molecule": row.get("molecule"),
            "dosage_form": row.get("dosage_form"),
            "nhi_type": row.get("nhi_type"),
        }
        recomputed_cd_id, candidates = strategic_brand.assign_cd_id(match_context, cd_markets_for_ml, filter_by_id)
        actual_cd_id = row.get("cd_id")
        if actual_cd_id != recomputed_cd_id or len(candidates) > 1:
            mismatches.append({"brand_id": brand_id, "actual_cd_id": actual_cd_id, "recomputed_cd_id": recomputed_cd_id, "candidates": ",".join(candidates), "reason": "q51_vs_cd_filter_mismatch"})
    return mismatches


def load_cd_brand_records(
    strategic_brand_path: Path,
    cd_market_path: Path,
    cd_filter_path: Path,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ingested_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    brand_rows = read_parquet_rows(strategic_brand_path)
    cd_market_rows = read_parquet_rows(cd_market_path)
    cd_filter_rows = read_parquet_rows(cd_filter_path)
    contexts = strategic_product.load_context_by_brand_id(input_file=input_file, catalog_path=catalog_path)
    mismatches = recompute_cd_assignments(brand_rows, cd_market_rows, cd_filter_rows, contexts)
    if mismatches:
        raise ValueError(f"Q-51 vs cd_filter cross-check mismatch: {mismatches[:10]}")
    timestamp = ingested_at or utc_now_datetime()
    cd_brand_rows = []
    for row in brand_rows:
        if row.get("cd_id") is None:
            continue
        out = {column: row.get(column) for column in strategic_brand.EXPECTED_COLUMNS}
        out["ingested_at"] = timestamp
        cd_brand_rows.append(out)
    validate_records(cd_brand_rows, cd_market_rows)
    return cd_brand_rows, mismatches


def validate_records(records: list[dict[str, Any]], cd_market_rows: list[dict[str, Any]]) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"cd_brand row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    expected_columns = tuple(strategic_brand.EXPECTED_COLUMNS)
    cd_ids = {str(row["cd_id"]) for row in cd_market_rows}
    brand_ids = [str(row["brand_id"]) for row in records]
    if len(set(brand_ids)) != len(brand_ids):
        raise ValueError("cd_brand.brand_id must be unique")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != expected_columns:
            raise ValueError(f"row {index} columns mismatch: expected={expected_columns}, actual={tuple(record.keys())}")
        if record["cd_id"] is None:
            raise ValueError(f"row {index} cd_id must be non-null")
        if str(record["cd_id"]) not in cd_ids:
            raise ValueError(f"row {index} missing cd_market FK: {record['cd_id']}")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"row {index} ingested_at must be datetime")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_typed_parquet(records, output_file, strategic_brand.STRATEGIC_BRAND_SCHEMA)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != strategic_brand.STRATEGIC_BRAND_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={strategic_brand.STRATEGIC_BRAND_SCHEMA}\nactual={table.schema}")
    if table.num_rows != EXPECTED_ROW_COUNT:
        raise ValueError(f"written row count mismatch: {table.num_rows}")
