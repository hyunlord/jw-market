from __future__ import annotations

import pyarrow as pa

EXPECTED_COLUMNS = (
    "product_id",
    "name",
    "merge_name",
    "brand_id",
    "ml_id",
    "cd_id",
    "class",
    "molecule",
    "molecule_raw",
    "dosage_form",
    "dosage_form_raw",
    "strength_pack",
    "nhi_type",
    "ox_gx",
    "fish_oil",
    "판매사",
    "제조사",
    "source_file_version",
    "ingested_at",
)

STRATEGIC_PRODUCT_SCHEMA = pa.schema(
    [
        pa.field("product_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("merge_name", pa.string(), nullable=False),
        pa.field("brand_id", pa.string(), nullable=False),
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("cd_id", pa.string(), nullable=True),
        pa.field("class", pa.string(), nullable=True),
        pa.field("molecule", pa.string(), nullable=True),
        pa.field("molecule_raw", pa.string(), nullable=True),
        pa.field("dosage_form", pa.string(), nullable=True),
        pa.field("dosage_form_raw", pa.string(), nullable=True),
        pa.field("strength_pack", pa.string(), nullable=True),
        pa.field("nhi_type", pa.string(), nullable=True),
        pa.field("ox_gx", pa.string(), nullable=True),
        pa.field("fish_oil", pa.string(), nullable=True),
        pa.field("판매사", pa.string(), nullable=True),
        pa.field("제조사", pa.string(), nullable=True),
        pa.field("source_file_version", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us"), nullable=False),
    ]
)

UBIST_JOIN_KEY_BY_SMID = {
    "strategy_001": "ubist_brand_manufacturer",
    "strategy_005": "ubist_brand_manufacturer",
    "strategy_006": "ubist_product_manufacturer",
    "strategy_007": "ubist_product_manufacturer",
    "strategy_008": "ubist_brand_manufacturer",
    "strategy_009": "ubist_brand_manufacturer",
    "strategy_015": "ubist_brand_manufacturer",
}

IQVIA_JOIN_KEY_BY_SMID = {
    "strategy_002": "iqvia_atc4_molecule",
    "strategy_003": "iqvia_atc4_molecule",
    "strategy_004": "iqvia_manufacturer_atc4_molecule",
    "strategy_010": "iqvia_manufacturer_atc4_molecule",
    "strategy_011": "iqvia_manufacturer_atc4_molecule",
    "strategy_012": "iqvia_atc4_molecule",
    "strategy_013": "iqvia_atc4_molecule",
    "strategy_014": "iqvia_atc4_molecule",
    "strategy_015": "iqvia_pack_manufacturer_atc4",
    "strategy_016": "iqvia_manufacturer_atc4_molecule",
}

import csv
from datetime import datetime
from pathlib import Path
from typing import Any


def validate_records(
    records: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    brand_rows: list[dict[str, Any]],
    ml_rows: list[dict[str, Any]],
    cd_rows: list[dict[str, Any]],
) -> None:
    if not records:
        raise ValueError("strategic_product must not be empty")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != EXPECTED_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={EXPECTED_COLUMNS}, actual={tuple(record.keys())}"
            )
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"row {index} ingested_at must be datetime")
    product_ids = [record["product_id"] for record in records]
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("product_id must be unique")

    brand_by_id = {str(row["brand_id"]): row for row in brand_rows}
    ml_ids = {str(row["ml_id"]) for row in ml_rows}
    cd_ids = {str(row["cd_id"]) for row in cd_rows}
    for record in records:
        brand = brand_by_id.get(str(record["brand_id"]))
        if brand is None:
            raise ValueError(f"{record['product_id']} missing brand FK: {record['brand_id']}")
        if record["ml_id"] not in ml_ids:
            raise ValueError(f"{record['product_id']} missing ml FK: {record['ml_id']}")
        if record["cd_id"] is not None and record["cd_id"] not in cd_ids:
            raise ValueError(f"{record['product_id']} missing cd FK: {record['cd_id']}")
        for column in ("merge_name", "ml_id", "cd_id"):
            if record[column] != brand[column]:
                raise ValueError(
                    f"{record['product_id']} {column} inheritance mismatch: "
                    f"product={record[column]!r}, brand={brand[column]!r}"
                )

    coverage_by_brand = {row["brand_id"]: row for row in coverage_rows}
    if set(coverage_by_brand) != set(brand_by_id):
        raise ValueError("coverage cache must have exactly one row per strategic_brand")
    fallback_count = sum(1 for row in coverage_rows if row["match_status"] == "fallback")
    if fallback_count == 0:
        raise ValueError("Q-52 fallback path was not exercised; review matching logic")

    expected_merge_names = {"엔브렐", "오렌시아", "젤잔즈"}
    for merge_name in expected_merge_names:
        product_names = {
            row["brand_id"]
            for row in records
            if row["merge_name"] == merge_name
        }
        brand_names = {
            row["brand_id"]
            for row in brand_rows
            if row["merge_name"] == merge_name
        }
        if not brand_names.issubset(product_names):
            raise ValueError(f"merge_name inheritance missing product rows for {merge_name}")

def write_coverage_cache(rows: list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "brand_id",
        "strategic_market_id",
        "ml_id",
        "cd_id",
        "brand_name",
        "data_source",
        "join_keys_attempted",
        "source_views_matched",
        "match_status",
        "matched_product_count",
        "sample_product_names",
    )
    with output_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
