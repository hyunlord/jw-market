from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from typing import Any

from pipeline.etl.io.catalog.cd_filter_schema import (
    CD_FILTER_COLUMNS,
    EXPECTED_CD_FILTER_IDS,
    EXPECTED_SOURCE_FILE_VERSION,
    FILTER_COLUMNS,
    JSON_ARRAY_COLUMNS,
    ML_EQUALS_CD_FILTER_IDS,
)
from pipeline.etl.io.catalog.cd_filter_specs import dumps_json_array
from pipeline.etl.io.catalog.market_catalog_text import clean_text

def validate_json_array_column(record: dict[str, Any], column: str) -> None:
    value = record[column]
    if value is None:
        return
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{record['cd_filter_id']} {column} must be non-empty JSON array")
    if any(not isinstance(item, str) or not item for item in parsed):
        raise ValueError(f"{record['cd_filter_id']} {column} contains invalid item: {parsed!r}")
    if json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) != value:
        raise ValueError(f"{record['cd_filter_id']} {column} is not canonical JSON array string")


def validate_records(records: list[dict[str, Any]]) -> None:
    if len(records) != 19:
        raise ValueError(f"cd_filter row count must be 19, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != CD_FILTER_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={CD_FILTER_COLUMNS}, actual={tuple(record.keys())}"
            )
    ids = [str(record["cd_filter_id"]) for record in records]
    if tuple(ids) != EXPECTED_CD_FILTER_IDS:
        raise ValueError(f"cd_filter_id sequence mismatch: actual={ids}")
    if len(set(ids)) != 19:
        raise ValueError("cd_filter_id must be unique")

    by_id = {str(record["cd_filter_id"]): record for record in records}

    for filter_id in ML_EQUALS_CD_FILTER_IDS:
        row = by_id[filter_id]
        populated = {column: row[column] for column in FILTER_COLUMNS if row[column] is not None}
        if populated:
            raise ValueError(f"{filter_id} must have all filter columns NULL, found={populated}")

    for record in records:
        for column in JSON_ARRAY_COLUMNS:
            validate_json_array_column(record, column)
        if clean_text(record["source_file_version"]) != unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION):
            raise ValueError(f"{record['cd_filter_id']} source_file_version mismatch")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"{record['cd_filter_id']} ingested_at must be datetime")

    expected_values = {
        ("cdf_005", "atc3"): dumps_json_array(["C1D"]),
        ("cdf_008", "class"): dumps_json_array(["Statin/ARB/CCB"]),
        ("cdf_009", "class"): dumps_json_array(["Statin/ARB"]),
        ("cdf_015", "dosage_form"): "IV Iron",
    }
    for (filter_id, column), expected in expected_values.items():
        actual = by_id[filter_id][column]
        if actual != expected:
            raise ValueError(f"{filter_id} {column} mismatch: expected={expected!r}, actual={actual!r}")



def count_non_null(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if record[column] is not None)
