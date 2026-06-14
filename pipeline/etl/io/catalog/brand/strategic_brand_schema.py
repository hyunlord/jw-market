from __future__ import annotations

import pyarrow as pa

from pipeline.etl.io.catalog._lib.expected_counts import expected_int, expected_mapping

EXPECTED_ROW_COUNT = expected_int("strategic_brand.row_count")
EXPECTED_STAGING_ROWS = expected_int("strategic_brand.staging_rows")
EXPECTED_EXCLUDED_ROWS = expected_int("strategic_brand.excluded_rows")
EXPECTED_COLUMNS = (
    "brand_id",
    "name",
    "merge_name",
    "ml_id",
    "cd_id",
    "is_excluded",
    "is_class_excluded",
    "allowed_atc4_codes_json",
    "class",
    "class_1",
    "class_2",
    "molecule",
    "dosage_form",
    "strength_pack",
    "nhi_type",
    "ox_gx",
    "fish_oil",
    "판매사",
    "제조사",
    "source_file_version",
    "ingested_at",
)
EXPECTED_ML_COUNTS = expected_mapping("strategic_brand.ml_counts")
SHEET_TOTAL_FILTER_IDS = {"cdf_004", "cdf_006", "cdf_007", "cdf_014", "cdf_016", "cdf_017"}
MERGE_NAME_BY_NAME = {
    "엔브렐마이클릭": "엔브렐",
    "엔브렐": "엔브렐",
    "오렌시아": "오렌시아",
    "오렌시아서브큐": "오렌시아",
    "젤잔즈": "젤잔즈",
    "젤잔즈엑스알": "젤잔즈",
}

STRATEGIC_BRAND_SCHEMA = pa.schema(
    [
        pa.field("brand_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("merge_name", pa.string(), nullable=False),
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("cd_id", pa.string(), nullable=True),
        pa.field("is_excluded", pa.bool_(), nullable=False),
        pa.field("is_class_excluded", pa.bool_(), nullable=False),
        pa.field("allowed_atc4_codes_json", pa.string(), nullable=True),
        pa.field("class", pa.string(), nullable=True),
        pa.field("class_1", pa.string(), nullable=True),
        pa.field("class_2", pa.string(), nullable=True),
        pa.field("molecule", pa.string(), nullable=True),
        pa.field("dosage_form", pa.string(), nullable=True),
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

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def validate_records(
    records: list[dict[str, Any]],
    summary: dict[str, Any],
    ml_rows: list[dict[str, Any]],
    cd_market_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"strategic_brand row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != EXPECTED_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={EXPECTED_COLUMNS}, actual={tuple(record.keys())}"
            )
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"row {index} ingested_at must be datetime")
    brand_ids = [record["brand_id"] for record in records]
    if len(set(brand_ids)) != len(brand_ids):
        raise ValueError("brand_id must be unique")

    ml_ids = {str(row["ml_id"]) for row in ml_rows}
    cd_ids = {str(row["cd_id"]) for row in cd_market_rows}
    for record in records:
        if record["ml_id"] not in ml_ids:
            raise ValueError(f"{record['brand_id']} missing ml FK: {record['ml_id']}")
        if record["cd_id"] is not None and record["cd_id"] not in cd_ids:
            raise ValueError(f"{record['brand_id']} missing cd FK: {record['cd_id']}")

    stats = summary["stats"]
    included_counts = dict(sorted(stats["included_rows"].items()))
    expected_by_smid = {
        f"strategy_{index:03d}": EXPECTED_ML_COUNTS[f"ml_{index:03d}"]
        for index in range(1, 17)
    }
    if included_counts != expected_by_smid:
        raise ValueError(f"market row distribution mismatch: expected={expected_by_smid}, actual={included_counts}")
    if sum(stats["excluded_rows"].values()) != EXPECTED_EXCLUDED_ROWS:
        raise ValueError(
            f"excluded rows must be {EXPECTED_EXCLUDED_ROWS}, found={sum(stats['excluded_rows'].values())}"
        )
    strict_excluded = sum(1 for record in records if record.get("is_excluded") is True)
    if strict_excluded != EXPECTED_EXCLUDED_ROWS:
        raise ValueError(f"is_excluded rows must be {EXPECTED_EXCLUDED_ROWS}, found={strict_excluded}")
    if sum(stats["included_rows"].values()) - sum(stats["excluded_rows"].values()) != EXPECTED_STAGING_ROWS:
        raise ValueError(f"included - strict_excluded must equal Phase 12 master_drug {EXPECTED_STAGING_ROWS} rows")
    if stats["overlap_rows"]:
        raise ValueError(f"Q-51 overlap rows found: {stats['overlap_rows'][:5]}")
    if stats["unknown_name_rows"]:
        raise ValueError(f"unknown brand name fallback rows found: {stats['unknown_name_rows'][:5]}")

    ml_counts = dict(sorted(Counter(record["ml_id"] for record in records).items()))
    if ml_counts != EXPECTED_ML_COUNTS:
        raise ValueError(f"ml distribution mismatch: expected={EXPECTED_ML_COUNTS}, actual={ml_counts}")

    merge_groups = defaultdict(set)
    for record in records:
        merge_groups[record["merge_name"]].add(record["name"])
    expected_merge_sets = {
        "엔브렐": {"엔브렐마이클릭", "엔브렐"},
        "오렌시아": {"오렌시아", "오렌시아서브큐"},
        "젤잔즈": {"젤잔즈", "젤잔즈엑스알"},
    }
    for merge_name, expected_names in expected_merge_sets.items():
        if merge_groups[merge_name] != expected_names:
            raise ValueError(
                f"merge_name {merge_name} mismatch: expected={expected_names}, actual={merge_groups[merge_name]}"
            )
    for merge_name, names in merge_groups.items():
        if merge_name not in expected_merge_sets and names != {merge_name}:
            raise ValueError(f"unexpected many-to-one merge_name mapping: {merge_name} -> {sorted(names)}")

    key_rows = {record["brand_id"]: record for record in records}
    for brand_id, expected_cd in {
        "sb_005_00017": "cd_005",
        "sb_008_00958": "cd_008",
        "sb_008_00978": "cd_008",
        "sb_008_01015": "cd_009",
    }.items():
        if brand_id in key_rows and key_rows[brand_id]["cd_id"] != expected_cd:
            raise ValueError(f"{brand_id} expected cd_id={expected_cd}, actual={key_rows[brand_id]['cd_id']}")


def write_gadrelet_cache(rows: list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "brand_id",
        "source_row_id",
        "atc4_code",
        "molecule",
        "class",
        "dosage_form",
        "strategic_brand_name",
        "cd_id",
    )
    with output_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
