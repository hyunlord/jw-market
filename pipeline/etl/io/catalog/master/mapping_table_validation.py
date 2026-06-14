from __future__ import annotations

from typing import Any

from pipeline.etl.io.catalog.master.mapping_table_schema import (
    EXPECTED_MAPPING_TYPE_DISTRIBUTION,
    EXPECTED_MARKET_DISTRIBUTION,
    EXPECTED_MARKET_STATS,
    EXPECTED_ROW_COUNT,
    MARKET_SHEETS,
    MARKET_SHEET_BY_ID,
    MASTER_MAPPING_TABLE_COLUMNS,
    ZERO_MAPPING_MARKETS,
    MarketMappingStats,
)


def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def validate_records(records: list[dict[str, Any]], stats: list[MarketMappingStats]) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"mapping row count must be {EXPECTED_ROW_COUNT}, found {len(records)}")

    mapping_ids = [record["mapping_id"] for record in records]
    if len(set(mapping_ids)) != EXPECTED_ROW_COUNT:
        duplicates = sorted({value for value in mapping_ids if mapping_ids.count(value) > 1})
        raise ValueError(f"mapping_id must be unique, duplicate examples={duplicates[:10]}")

    stats_by_market = {item.strategic_market_id: item for item in stats}
    expected_market_ids = {config.strategic_market_id for config in MARKET_SHEETS}
    if set(stats_by_market) != expected_market_ids:
        raise ValueError(
            f"market stats mismatch: expected={sorted(expected_market_ids)}, actual={sorted(stats_by_market)}"
        )

    for market_id, expected in EXPECTED_MARKET_STATS.items():
        actual = stats_by_market[market_id]
        sheet_config = MARKET_SHEET_BY_ID[market_id]
        for field, expected_value in (
            ("sheet_name", sheet_config.sheet_name),
            ("header_row", sheet_config.header_row),
        ):
            actual_value = getattr(actual, field)
            if actual_value != expected_value:
                raise ValueError(
                    f"{market_id} {field} mismatch: expected={expected_value}, actual={actual_value}"
                )
        for field in ("raw_rows_scanned", "empty_rows", "excluded_rows", "staging_rows", "manual_specs", "mapping_rows"):
            actual_value = getattr(actual, field)
            expected_value = expected[field]
            if actual_value != expected_value:
                raise ValueError(
                    f"{market_id} {field} mismatch: expected={expected_value}, actual={actual_value}"
                )

    zero_mapping_actual = {item.strategic_market_id for item in stats if item.mapping_rows == 0}
    if zero_mapping_actual != ZERO_MAPPING_MARKETS:
        raise ValueError(
            f"zero mapping market mismatch: expected={sorted(ZERO_MAPPING_MARKETS)}, "
            f"actual={sorted(zero_mapping_actual)}"
        )

    market_distribution = _count_by(records, "strategic_market_id")
    if market_distribution != EXPECTED_MARKET_DISTRIBUTION:
        raise ValueError(
            f"market distribution mismatch: expected={EXPECTED_MARKET_DISTRIBUTION}, "
            f"actual={market_distribution}"
        )

    type_distribution = _count_by(records, "mapping_type")
    if type_distribution != EXPECTED_MAPPING_TYPE_DISTRIBUTION:
        raise ValueError(
            f"mapping_type distribution mismatch: expected={EXPECTED_MAPPING_TYPE_DISTRIBUTION}, "
            f"actual={type_distribution}"
        )

    expected_columns = set(MASTER_MAPPING_TABLE_COLUMNS)
    for index, record in enumerate(records, start=1):
        extra_columns = sorted(set(record) - expected_columns)
        missing_columns = sorted(expected_columns - set(record))
        if extra_columns or missing_columns:
            raise ValueError(
                f"row {index} schema mismatch: extra={extra_columns}, missing={missing_columns}"
            )
        if not record["mapping_id"]:
            raise ValueError(f"row {index} has blank mapping_id")
        if not record["target_column"]:
            raise ValueError(f"row {index} has blank target_column: {record}")
        if record["mapping_type"] not in EXPECTED_MAPPING_TYPE_DISTRIBUTION:
            raise ValueError(f"row {index} unexpected mapping_type: {record['mapping_type']}")
