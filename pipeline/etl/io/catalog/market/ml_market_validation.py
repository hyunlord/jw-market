from __future__ import annotations

import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any

from pipeline.etl.io.catalog._lib.catalog_text import clean_text, parse_json_text
from pipeline.etl.io.catalog._lib.expected_counts import expected_int
from pipeline.etl.io.catalog.market.ml_market_schema import (
    ANALYZE_COLUMNS,
    ANALYZE_MATRIX,
    AUDIT_CODES,
    EXPECTED_DATA_SOURCE_COUNTS,
    EXPECTED_ML_IDS,
    EXPECTED_SOURCE_FILE_VERSION,
    EXPECTED_STRATEGY_005_SOURCE,
    ML_MARKET_COLUMNS,
)

EXPECTED_ROW_COUNT = expected_int("ml_market.row_count")

def analyze_values_for_ml(ml_id: str) -> dict[str, bool]:
    matrix = ANALYZE_MATRIX.get(ml_id)
    if matrix is None:
        raise ValueError(f"ANALYZE_MATRIX missing ml_id={ml_id}")
    return {f"analyze_{key}": bool(value) for key, value in matrix.items()}

def validate_records(records: list[dict[str, Any]]) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"ml_market row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != ML_MARKET_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={ML_MARKET_COLUMNS}, actual={tuple(record.keys())}"
            )

    ml_ids = [str(record["ml_id"]) for record in records]
    if tuple(ml_ids) != EXPECTED_ML_IDS:
        raise ValueError(f"ml_id sequence mismatch: actual={ml_ids}")
    if len(set(ml_ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("ml_id must be unique")

    data_source_counts = dict(Counter(str(record["data_source"]) for record in records))
    if data_source_counts != EXPECTED_DATA_SOURCE_COUNTS:
        raise ValueError(
            f"data_source distribution mismatch: "
            f"expected={EXPECTED_DATA_SOURCE_COUNTS}, actual={data_source_counts}"
        )
    strategy_005 = records[4]
    if strategy_005["ml_id"] != "ml_005" or strategy_005["data_source"] != EXPECTED_STRATEGY_005_SOURCE:
        raise ValueError(f"strategy_005 data_source must be ubist, found={strategy_005}")

    if set(ANALYZE_MATRIX) != set(EXPECTED_ML_IDS):
        raise ValueError(
            f"ANALYZE_MATRIX key mismatch: "
            f"expected={EXPECTED_ML_IDS}, actual={sorted(ANALYZE_MATRIX)}"
        )
    for record in records:
        expected = analyze_values_for_ml(str(record["ml_id"]))
        for column in ANALYZE_COLUMNS:
            if bool(record[column]) != expected[column]:
                raise ValueError(
                    f"{record['ml_id']} {column} mismatch: "
                    f"expected={expected[column]}, actual={record[column]}"
                )

    for record in records:
        atc_codes = parse_json_text(record.get("atc_codes_json"), [])
        if not isinstance(atc_codes, list) or any(clean_text(code) is None for code in atc_codes):
            raise ValueError(f"{record['ml_id']} atc_codes_json must be a JSON string list")
        source = str(record["data_source"])
        iqvia_values = [record[f"target_iqvia_{i}"] for i in range(1, 4)]
        ubist_values = [record[f"target_ubist_{i}"] for i in range(1, 5)]
        if source == "ubist" and any(value is not None for value in iqvia_values):
            raise ValueError(f"{record['ml_id']} UBIST-only row has IQVIA targets")
        if source == "iqvia" and any(value is not None for value in ubist_values):
            raise ValueError(f"{record['ml_id']} IQVIA-only row has UBIST targets")
        for value in iqvia_values:
            if value is not None and value not in AUDIT_CODES:
                raise ValueError(f"{record['ml_id']} invalid IQVIA audit code={value!r}")
        if clean_text(record["source_file_version"]) != unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION):
            raise ValueError(f"{record['ml_id']} source_file_version mismatch")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"{record['ml_id']} ingested_at must be datetime")

def count_true(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if bool(record[column]))


def nonnull_count(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if record.get(column) is not None)
