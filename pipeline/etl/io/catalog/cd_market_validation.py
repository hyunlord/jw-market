from __future__ import annotations

import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any

from pipeline.etl.io.catalog.cd_market_schema import (
    CD_MARKET_COLUMNS,
    EXPECTED_CD_IDS,
    EXPECTED_DATA_SOURCE_COUNTS,
    EXPECTED_SOURCE_FILE_VERSION,
)
from pipeline.etl.io.catalog.market_catalog_text import clean_market_text as clean_text

def validate_records(
    records: list[dict[str, Any]],
    ml_rows: list[dict[str, Any]],
    cd_filter_rows: list[dict[str, Any]],
) -> None:
    if len(records) != 19:
        raise ValueError(f"cd_market row count must be 19, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != CD_MARKET_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={CD_MARKET_COLUMNS}, actual={tuple(record.keys())}"
            )
    cd_ids = [str(record["cd_id"]) for record in records]
    if tuple(cd_ids) != EXPECTED_CD_IDS:
        raise ValueError(f"cd_id sequence mismatch: actual={cd_ids}")
    if len(set(cd_ids)) != 19:
        raise ValueError("cd_id must be unique")

    ml_ids = {str(row["ml_id"]) for row in ml_rows}
    cd_filter_ids = {str(row["cd_filter_id"]) for row in cd_filter_rows}
    for record in records:
        if str(record["ml_id"]) not in ml_ids:
            raise ValueError(f"{record['cd_id']} missing ml FK: {record['ml_id']}")
        if str(record["cd_filter_id"]) not in cd_filter_ids:
            raise ValueError(
                f"{record['cd_id']} missing cd_filter FK: {record['cd_filter_id']}"
            )
        if clean_text(record["source_file_version"]) != unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION):
            raise ValueError(f"{record['cd_id']} source_file_version mismatch")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"{record['cd_id']} ingested_at must be datetime")

    source_counts = dict(Counter(str(record["data_source"]) for record in records))
    if source_counts != EXPECTED_DATA_SOURCE_COUNTS:
        raise ValueError(
            f"data_source distribution mismatch: "
            f"expected={EXPECTED_DATA_SOURCE_COUNTS}, actual={source_counts}"
        )

    by_id = {str(record["cd_id"]): record for record in records}
    ml_by_id = {str(row["ml_id"]): row for row in ml_rows}
    inherited_columns = (
        "data_source",
        "analyze_class",
        "analyze_molecule",
        "analyze_dosage_form",
        "analyze_strength_pack",
        "analyze_nhi_type",
        "analyze_ox_gx",
        "analyze_fish_oil",
        "target_iqvia_1",
        "target_iqvia_2",
        "target_iqvia_3",
        "target_ubist_1",
        "target_ubist_2",
        "target_ubist_3",
        "target_ubist_4",
    )
    for cd_row in records:
        ml_row = ml_by_id[str(cd_row["ml_id"])]
        for column in inherited_columns:
            if cd_row[column] != ml_row[column]:
                raise ValueError(
                    f"{cd_row['cd_id']} ml_market inheritance mismatch for {column}: "
                    f"cd={cd_row[column]!r}, ml={ml_row[column]!r}"
                )

    expected_links = {
        "cd_008": ("ml_008", "cdf_008"),
        "cd_009": ("ml_008", "cdf_009"),
        "cd_010": ("ml_009", "cdf_010"),
        "cd_011": ("ml_009", "cdf_011"),
        "cd_012": ("ml_010", "cdf_012"),
        "cd_013": ("ml_010", "cdf_013"),
        "cd_015": ("ml_012", "cdf_015"),
        "cd_017": ("ml_015", "cdf_017"),
        "cd_018": ("ml_014", "cdf_018"),
    }
    for cd_id, (expected_ml, expected_filter) in expected_links.items():
        row = by_id[cd_id]
        if row["ml_id"] != expected_ml or row["cd_filter_id"] != expected_filter:
            raise ValueError(f"{cd_id} link mismatch: {row}")

    if not by_id["cd_018"]["analyze_fish_oil"]:
        raise ValueError("cd_018 analyze_fish_oil must be True")
    if not by_id["cd_002"]["analyze_nhi_type"]:
        raise ValueError("cd_002 analyze_nhi_type must be True from R19")
    if not by_id["cd_014"]["analyze_ox_gx"]:
        raise ValueError("cd_014 analyze_ox_gx must be True from R19")



def count_true(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if bool(record[column]))


def nonnull_count(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if record.get(column) is not None)
