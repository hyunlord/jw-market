"""
prototype_13_brand_group_split_to_parquet.py
============================================
Phase 12 Round 3 brand consolidation split -> Parquet.

Inputs:
- parquet/master_brand_consolidation/master_brand_consolidation.parquet
- parquet/master_drug/master_drug.parquet

Outputs:
- parquet/dim_brand_group/dim_brand_group.parquet
- parquet/master_brand_consolidation_members/master_brand_consolidation_members.parquet

Policy:
- Q-27 option B splits the old 6-row single table into a 3-row group
  dimension and a 6-row member table.
- All output columns are string dtype.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")


DEFAULT_BRAND_CONSOLIDATION_FILE = Path(
    "parquet/master_brand_consolidation/master_brand_consolidation.parquet"
)
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_DIM_BRAND_GROUP_FILE = Path("parquet/dim_brand_group/dim_brand_group.parquet")
DEFAULT_MEMBERS_FILE = Path(
    "parquet/master_brand_consolidation_members/master_brand_consolidation_members.parquet"
)

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx"
EXPECTED_STRATEGIC_MARKET_ID = "strategy_011"
EXPECTED_SOURCE_SHEET = "악템라"
EXPECTED_SOURCE_REMARK = "Master Remark indicates one-brand consolidation"

DIM_BRAND_GROUP_COLUMNS = (
    "brand_group_id",
    "strategic_market_id",
    "brand_group_name",
    "source_remark",
    "source_sheet",
    "source_file_version",
    "ingested_at",
)

MASTER_BRAND_CONSOLIDATION_MEMBER_COLUMNS = (
    "brand_group_id",
    "member_drug_index",
    "member_drug_name",
    "source_file_version",
    "ingested_at",
)

EXPECTED_GROUPS = (
    ("bg_001", "엔브렐"),
    ("bg_002", "오렌시아"),
    ("bg_003", "젤잔즈"),
)

EXPECTED_MEMBERS = (
    ("bg_001", "5", "엔브렐마이클릭"),
    ("bg_001", "6", "엔브렐"),
    ("bg_002", "18", "오렌시아"),
    ("bg_002", "19", "오렌시아서브큐"),
    ("bg_003", "22", "젤잔즈"),
    ("bg_003", "23", "젤잔즈엑스알"),
)

GROUP_NAME_BY_ID = dict(EXPECTED_GROUPS)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def _stringify_record(record: dict[str, Any], columns: tuple[str, ...]) -> dict[str, str | None]:
    return {column: None if record.get(column) is None else str(record.get(column)) for column in columns}


def validate_source_brand_consolidation(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 6:
        raise ValueError(f"source master_brand_consolidation row count must be 6, found={len(rows)}")

    expected_old_rows = {
        (EXPECTED_STRATEGIC_MARKET_ID, GROUP_NAME_BY_ID[group_id], drug_index, name)
        for group_id, drug_index, name in EXPECTED_MEMBERS
    }
    actual_old_rows = {
        (
            str(row.get("strategic_market_id")),
            str(row.get("brand_group")),
            str(row.get("member_drug_index")),
            str(row.get("member_drug_name")),
        )
        for row in rows
    }
    if actual_old_rows != expected_old_rows:
        raise ValueError(
            f"source master_brand_consolidation rows mismatch: "
            f"missing={sorted(expected_old_rows - actual_old_rows)}, "
            f"extra={sorted(actual_old_rows - expected_old_rows)}"
        )

    source_versions = {str(row.get("source_file_version")) for row in rows}
    source_sheets = {str(row.get("source_sheet")) for row in rows}
    source_remarks = {str(row.get("source_remark")) for row in rows}
    if source_versions != {EXPECTED_SOURCE_FILE_VERSION}:
        raise ValueError(f"source_file_version mismatch: {source_versions}")
    if source_sheets != {EXPECTED_SOURCE_SHEET}:
        raise ValueError(f"source_sheet mismatch: {source_sheets}")
    if source_remarks != {EXPECTED_SOURCE_REMARK}:
        raise ValueError(f"source_remark mismatch: {source_remarks}")


def make_group_records(ingested_at: str) -> list[dict[str, str | None]]:
    records = []
    for brand_group_id, brand_group_name in EXPECTED_GROUPS:
        records.append(
            {
                "brand_group_id": brand_group_id,
                "strategic_market_id": EXPECTED_STRATEGIC_MARKET_ID,
                "brand_group_name": brand_group_name,
                "source_remark": EXPECTED_SOURCE_REMARK,
                "source_sheet": EXPECTED_SOURCE_SHEET,
                "source_file_version": EXPECTED_SOURCE_FILE_VERSION,
                "ingested_at": ingested_at,
            }
        )
    return records


def make_member_records(ingested_at: str) -> list[dict[str, str | None]]:
    records = []
    for brand_group_id, member_drug_index, member_drug_name in EXPECTED_MEMBERS:
        records.append(
            {
                "brand_group_id": brand_group_id,
                "member_drug_index": member_drug_index,
                "member_drug_name": member_drug_name,
                "source_file_version": EXPECTED_SOURCE_FILE_VERSION,
                "ingested_at": ingested_at,
            }
        )
    return records


def validate_outputs(
    group_records: list[dict[str, str | None]],
    member_records: list[dict[str, str | None]],
    master_drug_rows: list[dict[str, Any]],
) -> None:
    if len(group_records) != 3:
        raise ValueError(f"dim_brand_group row count must be 3, found={len(group_records)}")
    if len(member_records) != 6:
        raise ValueError(
            f"master_brand_consolidation_members row count must be 6, found={len(member_records)}"
        )

    for index, record in enumerate(group_records, start=1):
        if tuple(record.keys()) != DIM_BRAND_GROUP_COLUMNS:
            raise ValueError(f"group row {index} columns mismatch: {tuple(record.keys())}")
    for index, record in enumerate(member_records, start=1):
        if tuple(record.keys()) != MASTER_BRAND_CONSOLIDATION_MEMBER_COLUMNS:
            raise ValueError(f"member row {index} columns mismatch: {tuple(record.keys())}")

    group_ids = [record["brand_group_id"] for record in group_records]
    if len(set(group_ids)) != 3:
        raise ValueError("dim_brand_group brand_group_id must be unique")

    group_natural_keys = [
        (record["strategic_market_id"], record["brand_group_name"]) for record in group_records
    ]
    if len(set(group_natural_keys)) != 3:
        raise ValueError("dim_brand_group natural key must be unique")

    member_pks = [
        (record["brand_group_id"], record["member_drug_index"]) for record in member_records
    ]
    if len(set(member_pks)) != 6:
        raise ValueError("member PK (brand_group_id, member_drug_index) must be unique")

    valid_group_ids = set(group_ids)
    unknown_group_ids = sorted(
        {record["brand_group_id"] for record in member_records} - valid_group_ids
    )
    if unknown_group_ids:
        raise ValueError(f"member FK to dim_brand_group failed: {unknown_group_ids}")

    master_drug_by_key = {
        (str(row.get("strategic_market_id")), str(row.get("drug_index"))): str(row.get("product_name"))
        for row in master_drug_rows
    }
    for record in member_records:
        key = (EXPECTED_STRATEGIC_MARKET_ID, str(record["member_drug_index"]))
        actual_product_name = master_drug_by_key.get(key)
        if actual_product_name != record["member_drug_name"]:
            raise ValueError(
                f"member FK to master_drug failed: {key} expected={record['member_drug_name']!r} "
                f"actual={actual_product_name!r}"
            )


def write_parquet(records: list[dict[str, Any]], columns: tuple[str, ...], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([pa.field(column, pa.string()) for column in columns])
    table = pa.Table.from_pylist([_stringify_record(record, columns) for record in records], schema=schema)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 12 brand group split parquet.")
    parser.add_argument("--brand-consolidation", type=Path, default=DEFAULT_BRAND_CONSOLIDATION_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--dim-brand-group-output", type=Path, default=DEFAULT_DIM_BRAND_GROUP_FILE)
    parser.add_argument("--members-output", type=Path, default=DEFAULT_MEMBERS_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_rows = read_parquet_rows(args.brand_consolidation)
    master_drug_rows = read_parquet_rows(args.master_drug)
    validate_source_brand_consolidation(source_rows)

    timestamp = utc_now_text()
    group_records = make_group_records(timestamp)
    member_records = make_member_records(timestamp)
    validate_outputs(group_records, member_records, master_drug_rows)

    write_parquet(group_records, DIM_BRAND_GROUP_COLUMNS, args.dim_brand_group_output)
    write_parquet(member_records, MASTER_BRAND_CONSOLIDATION_MEMBER_COLUMNS, args.members_output)

    print("prototype Phase 12 Round 3 brand consolidation split -> Parquet")
    print(f"dim_brand_group_rows={len(group_records)}")
    print(f"master_brand_consolidation_members_rows={len(member_records)}")
    print(f"dim_brand_group_output={args.dim_brand_group_output}")
    print(f"members_output={args.members_output}")
    print(f"source_file_version={EXPECTED_SOURCE_FILE_VERSION}")
    print(f"ingested_at={timestamp}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
