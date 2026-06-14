from __future__ import annotations

import json
from typing import Any

from pipeline.etl.io.catalog.master.market_definition_schema import (
    EXPECTED_ROW_COUNT,
    EXPECTED_STRATEGIC_MARKET_IDS,
    MASTER_MARKET_DEFINITION_COLUMNS,
)


def validate_records(records: list[dict[str, Any]]) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"market_definition row count must be {EXPECTED_ROW_COUNT}, found {len(records)}")

    ids = [record["strategic_market_id"] for record in records]
    if len(set(ids)) != EXPECTED_ROW_COUNT:
        raise ValueError(f"strategic_market_id must be unique, found duplicate ids: {ids}")
    if tuple(ids) != EXPECTED_STRATEGIC_MARKET_IDS:
        raise ValueError(
            "strategic_market_id order/mapping mismatch: "
            f"expected={EXPECTED_STRATEGIC_MARKET_IDS}, actual={tuple(ids)}"
        )

    for index, record in enumerate(records, start=1):
        extra_columns = sorted(set(record) - set(MASTER_MARKET_DEFINITION_COLUMNS))
        missing_columns = sorted(set(MASTER_MARKET_DEFINITION_COLUMNS) - set(record))
        if extra_columns or missing_columns:
            raise ValueError(
                f"row {index} schema mismatch: extra={extra_columns}, missing={missing_columns}"
            )
        for column in (
            "market_atc_codes_json",
            "full_market_atc4_codes_json",
            "direct_competition_brands_json",
            "analysis_levels_json",
            "target_customer_priority_json",
            "raw_row_json",
        ):
            json.loads(record[column])
