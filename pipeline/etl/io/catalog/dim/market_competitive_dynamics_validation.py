from __future__ import annotations

import json
from collections import Counter
from typing import Any

from pipeline.etl.io.catalog._lib.common import clean_text
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_schema import (
    DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS,
    EXPECTED_CD_COUNTS,
    EXPECTED_CD_IDS,
    EXPECTED_DEFINITION_TYPE_COUNTS,
    EXPECTED_ROW_COUNT,
    EXPECTED_STRATEGY_008_CLASS2_NON_NULL_COUNT,
    EXPECTED_STRATEGY_008_NON_CD_CLASS2_COUNT,
    EXPECTED_TOTAL_CD_BRAND_COUNT,
)

def validate_records(
    records: list[dict[str, Any]],
    dim_market_landscape_rows: list[dict[str, Any]],
    market_definition_rows: list[dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS}, "
                f"actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"row {index} column {column} must be string/None, got={type(value)}")

    cd_ids = [record["competitive_dynamics_id"] for record in records]
    if tuple(cd_ids) != EXPECTED_CD_IDS:
        raise ValueError(f"competitive_dynamics_id sequence mismatch: {cd_ids}")
    if len(set(cd_ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("competitive_dynamics_id must be unique")

    landscape_ids = {str(row["market_landscape_id"]) for row in dim_market_landscape_rows}
    market_ids = {str(row["strategic_market_id"]) for row in market_definition_rows}
    for record in records:
        if record["parent_market_landscape_id"] not in landscape_ids:
            raise ValueError(f"missing landscape FK: {record['parent_market_landscape_id']}")
        if record["strategic_market_id"] not in market_ids:
            raise ValueError(f"missing market FK: {record['strategic_market_id']}")

    definition_counts = Counter(record["cd_definition_type"] for record in records)
    for definition_type, expected_count in EXPECTED_DEFINITION_TYPE_COUNTS.items():
        if definition_counts[definition_type] < expected_count:
            raise ValueError(
                f"cd_definition_type baseline mismatch for {definition_type}: "
                f"expected_at_least={expected_count}, actual={definition_counts[definition_type]}"
            )

    cd_counts = {
        str(record["competitive_dynamics_id"]): int(str(record["cd_brand_count"]))
        for record in records
    }
    for cd_id, expected_count in EXPECTED_CD_COUNTS.items():
        if cd_counts.get(cd_id) != expected_count:
            raise ValueError(
                f"cd_brand_count mismatch for {cd_id}: "
                f"expected={expected_count}, actual={cd_counts.get(cd_id)}"
            )
    if sum(EXPECTED_CD_COUNTS.values()) != EXPECTED_TOTAL_CD_BRAND_COUNT:
        raise ValueError("historical cd_brand baseline total is internally inconsistent")

    for record in records:
        cd_id = str(record["competitive_dynamics_id"])
        for json_column in (
            "cd_filter_raw_json",
            "cd_brand_list_json",
            "target_customer_priority_raw_json",
            "analysis_levels_json",
        ):
            try:
                parsed = json.loads(str(record[json_column]))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{cd_id} {json_column} invalid JSON: {exc}") from exc
            if json_column != "cd_brand_list_json" and not isinstance(parsed, list):
                raise ValueError(f"{cd_id} {json_column} must be a JSON array")

        brand_payload = json.loads(str(record["cd_brand_list_json"]))
        brand_count = int(str(record["cd_brand_count"]))
        if brand_payload.get("row_count") != brand_count:
            raise ValueError(f"{cd_id} cd_brand_list_json row_count mismatch")
        brands = brand_payload.get("brands")
        if not isinstance(brands, list) or len(brands) != brand_count:
            raise ValueError(f"{cd_id} cd_brand_list_json brands length mismatch")
        for brand in brands:
            if tuple(brand.keys()) != ("drug_index", "pack", "product_name", "strength"):
                raise ValueError(f"{cd_id} brand shape mismatch: {brand}")
            if not isinstance(brand["drug_index"], int):
                raise ValueError(f"{cd_id} drug_index must be int inside JSON")

    strategy_008_class2_counts = Counter(
        clean_text(row.get("class_2"))
        for row in master_drug_rows
        if str(row.get("strategic_market_id")) == "strategy_008"
        and clean_text(row.get("class_2")) is not None
    )
    if sum(strategy_008_class2_counts.values()) != EXPECTED_STRATEGY_008_CLASS2_NON_NULL_COUNT:
        raise ValueError(
            f"strategy_008 class_2 non-null count must be "
            f"{EXPECTED_STRATEGY_008_CLASS2_NON_NULL_COUNT}: {strategy_008_class2_counts}"
        )
    other_strategy_008 = sum(strategy_008_class2_counts.values()) - (
        EXPECTED_CD_COUNTS["cd_008"] + EXPECTED_CD_COUNTS["cd_009"]
    )
    if other_strategy_008 != EXPECTED_STRATEGY_008_NON_CD_CLASS2_COUNT:
        raise ValueError(
            f"strategy_008 non-CD class_2 count must be "
            f"{EXPECTED_STRATEGY_008_NON_CD_CLASS2_COUNT}, found={other_strategy_008}"
        )
