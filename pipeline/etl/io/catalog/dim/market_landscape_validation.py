from __future__ import annotations

import json
from collections import Counter
from typing import Any

from pipeline.etl.io.catalog._lib.common import clean_text
from pipeline.etl.io.catalog.dim.market_landscape_schema import (
    DEFAULT_SHEET_ALL_MARKETS,
    DIM_MARKET_LANDSCAPE_COLUMNS,
    EXPECTED_DEFINITION_TYPE_COUNTS,
    EXPECTED_MARKET_COUNTS,
    EXPECTED_ROW_COUNT,
    EXPECTED_TOTAL_MASTER_DRUG_ROWS,
)

def validate_records(
    records: list[dict[str, Any]],
    market_definition_rows: list[dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"dim_market_landscape row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")

    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_LANDSCAPE_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={DIM_MARKET_LANDSCAPE_COLUMNS}, actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"row {index} column {column} must be string/None, got={type(value)}"
                )

    market_landscape_ids = [record["market_landscape_id"] for record in records]
    expected_ml_ids = [f"ml_{index:03d}" for index in range(1, EXPECTED_ROW_COUNT + 1)]
    if market_landscape_ids != expected_ml_ids:
        raise ValueError(
            f"market_landscape_id sequence mismatch: expected={expected_ml_ids}, "
            f"actual={market_landscape_ids}"
        )
    if len(set(market_landscape_ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("market_landscape_id must be unique")

    market_definition_ids = {str(row.get("strategic_market_id")) for row in market_definition_rows}
    record_ids = [str(record["strategic_market_id"]) for record in records]
    if set(record_ids) != market_definition_ids:
        raise ValueError("strategic_market_id FK to master_market_definition failed")

    master_counts = Counter(str(row.get("strategic_market_id")) for row in master_drug_rows)
    for market_id, expected_count in EXPECTED_MARKET_COUNTS.items():
        if master_counts[market_id] != expected_count:
            raise ValueError(
                f"master_drug market count mismatch for {market_id}: "
                f"expected={expected_count}, actual={master_counts[market_id]}"
            )
    extra_master_counts = {
        market_id: count
        for market_id, count in master_counts.items()
        if market_id not in EXPECTED_MARKET_COUNTS
    }
    if any(count <= 0 for count in extra_master_counts.values()):
        raise ValueError(f"new market must contain at least one master drug row: {extra_master_counts}")
    if sum(EXPECTED_MARKET_COUNTS.values()) != EXPECTED_TOTAL_MASTER_DRUG_ROWS:
        raise ValueError("historical master_drug baseline total is internally inconsistent")

    record_counts = {
        str(record["strategic_market_id"]): int(str(record["ml_brand_count"]))
        for record in records
    }
    if record_counts != dict(master_counts):
        raise ValueError(
            f"ml_brand_count mismatch: expected={dict(master_counts)}, actual={record_counts}"
        )

    definition_type_counts = Counter(str(record["ml_definition_type"]) for record in records)
    for definition_type, expected_count in EXPECTED_DEFINITION_TYPE_COUNTS.items():
        if definition_type_counts[definition_type] < expected_count:
            raise ValueError(
                f"ml_definition_type baseline mismatch for {definition_type}: "
                f"expected_at_least={expected_count}, actual={definition_type_counts[definition_type]}"
            )
    if sum(definition_type_counts.values()) != len(records):
        raise ValueError(
            f"ml_definition_type distribution does not cover every record: "
            f"{dict(definition_type_counts)}"
        )
    default_ids = {
        str(record["strategic_market_id"])
        for record in records
        if record["ml_definition_type"] == "default_sheet_all"
    }
    if default_ids != DEFAULT_SHEET_ALL_MARKETS:
        raise ValueError(
            f"default_sheet_all markets mismatch: expected={sorted(DEFAULT_SHEET_ALL_MARKETS)}, "
            f"actual={sorted(default_ids)}"
        )

    master_product_by_key = {
        (str(row.get("strategic_market_id")), int(str(row.get("drug_index")))): clean_text(
            row.get("product_name")
        )
        for row in master_drug_rows
    }
    for record in records:
        strategic_market_id = str(record["strategic_market_id"])
        ml_brand_count = int(str(record["ml_brand_count"]))
        for json_column in (
            "ml_atc_codes_json",
            "ml_brand_list_json",
            "analysis_metrics_json",
        ):
            try:
                parsed = json.loads(str(record[json_column]))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{strategic_market_id} {json_column} invalid JSON: {exc}") from exc
            if json_column in ("ml_atc_codes_json", "analysis_metrics_json") and not isinstance(parsed, list):
                raise ValueError(f"{strategic_market_id} {json_column} must be a JSON array")

        brand_payload = json.loads(str(record["ml_brand_list_json"]))
        if brand_payload.get("row_count") != ml_brand_count:
            raise ValueError(
                f"{strategic_market_id} ml_brand_list_json row_count mismatch: "
                f"{brand_payload.get('row_count')} != {ml_brand_count}"
            )
        brands = brand_payload.get("brands")
        if not isinstance(brands, list) or len(brands) != ml_brand_count:
            raise ValueError(
                f"{strategic_market_id} ml_brand_list_json brands length mismatch: "
                f"{len(brands) if isinstance(brands, list) else 'not-list'} != {ml_brand_count}"
            )
        expected_indexes = list(range(1, ml_brand_count + 1))
        actual_indexes = [int(brand["drug_index"]) for brand in brands]
        if actual_indexes != expected_indexes:
            raise ValueError(
                f"{strategic_market_id} drug_index sequence mismatch in ml_brand_list_json"
            )
        for brand in brands:
            key = (strategic_market_id, int(brand["drug_index"]))
            if master_product_by_key.get(key) != brand.get("product_name"):
                raise ValueError(
                    f"{strategic_market_id} drug_index={brand['drug_index']} product_name mismatch: "
                    f"json={brand.get('product_name')!r}, master={master_product_by_key.get(key)!r}"
                )
