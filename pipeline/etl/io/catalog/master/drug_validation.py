from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.master.drug_records import load_column_metadata_catalog
from pipeline.etl.io.catalog.master.drug_schema import (
    EXPECTED_EXCLUDED_ROWS,
    EXPECTED_MARKET_STATS,
    EXPECTED_ROW_COUNT,
    EXPECTED_SOURCE_TYPE_DISTRIBUTION,
    JSON_COLUMNS,
    MARKET_SHEETS,
    MARKET_SHEET_BY_ID,
    MASTER_DRUG_COLUMNS,
    MarketDrugStats,
)
from pipeline.etl.io.catalog._lib.common import STANDARD_PREFIX, dumps_json


def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _expected_extra_keys(metadata: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(key.split(".", 1)[1] for key in metadata if key.startswith(STANDARD_PREFIX))


def validate_records(
    records: list[dict[str, Any]],
    stats: list[MarketDrugStats],
    catalog_path: Path,
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"master_drug row count must be {EXPECTED_ROW_COUNT}, found {len(records)}")

    expected_columns = set(MASTER_DRUG_COLUMNS)
    for index, record in enumerate(records, start=1):
        extra_columns = sorted(set(record) - expected_columns)
        missing_columns = sorted(expected_columns - set(record))
        if extra_columns or missing_columns:
            raise ValueError(f"row {index} schema mismatch: extra={extra_columns}, missing={missing_columns}")

    stats_by_market = {item.strategic_market_id: item for item in stats}
    expected_market_ids = {config.strategic_market_id for config in MARKET_SHEETS}
    if set(stats_by_market) != expected_market_ids:
        raise ValueError(
            f"market stats mismatch: expected={sorted(expected_market_ids)}, actual={sorted(stats_by_market)}"
        )

    total_excluded = 0
    for market_id, expected in EXPECTED_MARKET_STATS.items():
        actual = stats_by_market[market_id]
        sheet_config = MARKET_SHEET_BY_ID[market_id]
        for field, expected_value in (
            ("sheet_name", sheet_config.sheet_name),
            ("header_row", sheet_config.header_row),
        ):
            actual_value = getattr(actual, field)
            if actual_value != expected_value:
                raise ValueError(f"{market_id} {field} mismatch: expected={expected_value}, actual={actual_value}")
        for field in ("raw_rows_scanned", "empty_rows", "excluded_rows", "staging_rows"):
            actual_value = getattr(actual, field)
            expected_value = expected[field]
            if actual_value != expected_value:
                raise ValueError(f"{market_id} {field} mismatch: expected={expected_value}, actual={actual_value}")
        total_excluded += actual.excluded_rows
    if total_excluded != EXPECTED_EXCLUDED_ROWS:
        raise ValueError(f"excluded rows must be {EXPECTED_EXCLUDED_ROWS}, found {total_excluded}")

    pk_values = [(record["strategic_market_id"], str(record["drug_index"])) for record in records]
    if len(set(pk_values)) != EXPECTED_ROW_COUNT:
        duplicate_keys = sorted({value for value in pk_values if pk_values.count(value) > 1})
        raise ValueError(f"compound PK must be unique, duplicate examples={duplicate_keys[:10]}")

    market_distribution = _count_by(records, "strategic_market_id")
    expected_market_distribution = {
        market_id: expected["staging_rows"] for market_id, expected in EXPECTED_MARKET_STATS.items()
    }
    if market_distribution != expected_market_distribution:
        raise ValueError(
            f"market distribution mismatch: expected={expected_market_distribution}, actual={market_distribution}"
        )

    source_type_distribution = _count_by(records, "source_type")
    if source_type_distribution != EXPECTED_SOURCE_TYPE_DISTRIBUTION:
        raise ValueError(
            f"source_type distribution mismatch: expected={EXPECTED_SOURCE_TYPE_DISTRIBUTION}, "
            f"actual={source_type_distribution}"
        )

    metadata_catalog = load_column_metadata_catalog(catalog_path)
    records_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        records_by_market[record["strategic_market_id"]].append(record)

    for config in MARKET_SHEETS:
        market_records = records_by_market[config.strategic_market_id]
        expected_count = EXPECTED_MARKET_STATS[config.strategic_market_id]["staging_rows"]
        drug_indexes = [int(record["drug_index"]) for record in market_records]
        if drug_indexes != list(range(1, expected_count + 1)):
            raise ValueError(
                f"{config.strategic_market_id} drug_index sequence mismatch: "
                f"expected=1..{expected_count}, actual_first_last={drug_indexes[:3]}...{drug_indexes[-3:]}"
            )

        metadata = metadata_catalog[config.strategic_market_id]
        expected_metadata_json = dumps_json(metadata)
        expected_extra_keys = _expected_extra_keys(metadata)
        for record in market_records:
            for column in JSON_COLUMNS:
                try:
                    json.loads(record[column])
                except Exception as exc:
                    raise ValueError(f"{config.strategic_market_id} {column} is not valid JSON") from exc

            extra = json.loads(record["drug_extra_json"])
            raw_payload = json.loads(record["raw_row_json"])
            metadata_json = json.loads(record["column_metadata_json"])

            if sorted(extra.keys()) != expected_extra_keys:
                raise ValueError(
                    f"{config.strategic_market_id} drug_extra_json keys mismatch: "
                    f"expected={expected_extra_keys}, actual={sorted(extra.keys())}"
                )
            if record["column_metadata_json"] != expected_metadata_json:
                raise ValueError(f"{config.strategic_market_id} column_metadata_json string mismatch")
            if metadata_json != metadata:
                raise ValueError(f"{config.strategic_market_id} column_metadata_json structure mismatch")
            # 시트별 컬럼 수가 달라져도 raw_row_json의 cells와 values_by_header가
            # 같은 폭을 유지하는지만 검증한다. 26칸 고정 검사는 260518의 컬럼
            # 배치 변화를 버그로 오판하므로 기각했다.
            expected_cell_count = len(raw_payload.get("values_by_header", {}))
            if len(raw_payload.get("cells", [])) != expected_cell_count:
                raise ValueError(
                    f"{config.strategic_market_id} raw_row_json cells length mismatch: "
                    f"expected={expected_cell_count}, actual={len(raw_payload.get('cells', []))}"
                )
            if int(record["source_row_id"]) != int(raw_payload.get("source_row_id")):
                raise ValueError(f"{config.strategic_market_id} source_row_id mismatch in raw_row_json")
