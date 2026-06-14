from __future__ import annotations

from typing import Any

from pipeline.etl.io.catalog.master.brand_consolidation_schema import (
    EXPECTED_BRAND_GROUP_COUNTS,
    EXPECTED_DRUG_ROWS,
    EXPECTED_MEMBER_DRUG_INDEXES,
    EXPECTED_ROW_COUNT,
    MASTER_BRAND_CONSOLIDATION_COLUMNS,
    SOURCE_REMARK,
    SOURCE_SHEET,
    STRATEGIC_MARKET_ID,
    BrandConsolidationStats,
)


def validate_records(records: list[dict[str, Any]], stats: BrandConsolidationStats) -> None:
    if stats.staging_drug_rows != EXPECTED_DRUG_ROWS:
        raise ValueError(
            f"staging drug rows must be {EXPECTED_DRUG_ROWS}, found {stats.staging_drug_rows}"
        )
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(
            f"brand consolidation row count must be {EXPECTED_ROW_COUNT}, found {len(records)}"
        )

    pk_values = [
        (record["strategic_market_id"], record["brand_group"], record["member_drug_index"])
        for record in records
    ]
    if len(set(pk_values)) != EXPECTED_ROW_COUNT:
        raise ValueError(f"compound PK must be unique, found duplicates in {pk_values}")

    member_indexes = {int(record["member_drug_index"]) for record in records}
    if member_indexes != EXPECTED_MEMBER_DRUG_INDEXES:
        raise ValueError(
            f"member_drug_index mismatch: "
            f"expected={sorted(EXPECTED_MEMBER_DRUG_INDEXES)}, actual={sorted(member_indexes)}"
        )

    group_counts: dict[str, int] = {}
    for record in records:
        group_counts[record["brand_group"]] = group_counts.get(record["brand_group"], 0) + 1
    if group_counts != EXPECTED_BRAND_GROUP_COUNTS:
        raise ValueError(
            f"brand_group distribution mismatch: "
            f"expected={EXPECTED_BRAND_GROUP_COUNTS}, actual={group_counts}"
        )

    expected_columns = set(MASTER_BRAND_CONSOLIDATION_COLUMNS)
    for index, record in enumerate(records, start=1):
        extra_columns = sorted(set(record) - expected_columns)
        missing_columns = sorted(expected_columns - set(record))
        if extra_columns or missing_columns:
            raise ValueError(
                f"row {index} schema mismatch: extra={extra_columns}, missing={missing_columns}"
            )
        if record["strategic_market_id"] != STRATEGIC_MARKET_ID:
            raise ValueError(f"row {index} strategic_market_id mismatch: {record}")
        if record["source_sheet"] != SOURCE_SHEET:
            raise ValueError(f"row {index} source_sheet mismatch: {record}")
        if record["source_remark"] != SOURCE_REMARK:
            raise ValueError(f"row {index} source_remark mismatch: {record}")
