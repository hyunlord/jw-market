from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pipeline.etl.io.catalog.target_priority_schema import (
    AUTO_FILL_CACHE_COLUMNS,
    DIM_MARKET_TARGET_PRIORITY_COLUMNS,
    EXPECTED_BOTH_SOURCE_VIEW_CDS,
    EXPECTED_ROW_COUNT,
    EXPECTED_SOURCE_TYPE_COUNTS,
    EXPECTED_SOURCE_VIEW_COUNTS,
)

def validate_records(
    records: list[dict[str, Any]],
    dim_competitive_rows: list[dict[str, Any]],
    cache_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_TARGET_PRIORITY_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={DIM_MARKET_TARGET_PRIORITY_COLUMNS}, "
                f"actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"row {index} column {column} must be string/None, got={type(value)}")

    target_ids = [record["target_priority_id"] for record in records]
    expected_ids = [f"tp_{index:03d}" for index in range(1, EXPECTED_ROW_COUNT + 1)]
    if target_ids != expected_ids:
        raise ValueError(f"target_priority_id sequence mismatch: {target_ids}")
    if len(set(target_ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("target_priority_id must be unique")

    unique_keys = [
        (
            record["competitive_dynamics_id"],
            record["source_view"],
            record["priority_rank"],
        )
        for record in records
    ]
    if len(set(unique_keys)) != EXPECTED_ROW_COUNT:
        duplicates = [key for key, count in Counter(unique_keys).items() if count > 1]
        raise ValueError(f"(cd_id, source_view, priority_rank) duplicates: {duplicates}")

    cd_ids = {str(row["competitive_dynamics_id"]) for row in dim_competitive_rows}
    for record in records:
        if record["competitive_dynamics_id"] not in cd_ids:
            raise ValueError(f"missing competitive_dynamics FK: {record['competitive_dynamics_id']}")

    source_view_counts = dict(Counter(record["source_view"] for record in records))
    if source_view_counts != EXPECTED_SOURCE_VIEW_COUNTS:
        raise ValueError(
            f"source_view distribution mismatch: expected={EXPECTED_SOURCE_VIEW_COUNTS}, "
            f"actual={source_view_counts}"
        )
    source_type_counts = dict(Counter(record["source_type"] for record in records))
    if source_type_counts != EXPECTED_SOURCE_TYPE_COUNTS:
        raise ValueError(
            f"source_type distribution mismatch: expected={EXPECTED_SOURCE_TYPE_COUNTS}, "
            f"actual={source_type_counts}"
        )

    ranks_by_cd_source: dict[tuple[str, str], list[str]] = defaultdict(list)
    source_views_by_cd: dict[str, set[str]] = defaultdict(set)
    for record in records:
        cd_id = str(record["competitive_dynamics_id"])
        source_view = str(record["source_view"])
        ranks_by_cd_source[(cd_id, source_view)].append(str(record["priority_rank"]))
        source_views_by_cd[cd_id].add(source_view)
    for key, ranks in ranks_by_cd_source.items():
        if sorted(ranks, key=int) != ["1", "2", "3", "4"]:
            raise ValueError(f"priority_rank must be 1-4 for {key}: {ranks}")
    both_source_view_cds = {
        cd_id for cd_id, source_views in source_views_by_cd.items() if len(source_views) == 2
    }
    if both_source_view_cds != EXPECTED_BOTH_SOURCE_VIEW_CDS:
        raise ValueError(
            f"BOTH source_view CD mismatch: expected={EXPECTED_BOTH_SOURCE_VIEW_CDS}, "
            f"actual={both_source_view_cds}"
        )

    if len(cache_rows) != EXPECTED_SOURCE_TYPE_COUNTS["auto_fill_top_n_by_sales"]:
        raise ValueError(f"auto-fill cache row count mismatch: {len(cache_rows)}")
    for cache_row in cache_rows:
        if set(cache_row.keys()) != set(AUTO_FILL_CACHE_COLUMNS):
            raise ValueError(f"auto-fill cache shape mismatch: {cache_row.keys()}")
