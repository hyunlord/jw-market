from __future__ import annotations

import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from pipeline.etl.mi_master_registry import (
    default_mi_master_registry,
)
from pipeline.etl.io.catalog._lib.catalog_text import clean_market_text as clean_text

DEFAULT_ML_MARKET_FILE = Path("parquet/ml_market/ml_market.parquet")
DEFAULT_CD_FILTER_FILE = Path("parquet/cd_filter/cd_filter.parquet")
DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_OUTPUT_FILE = Path("parquet/cd_market/cd_market.parquet")

# cd_market 19개 정의도 260518 MI Master가 기준이다.
# 이 상수는 CD 시장 수, target priority, view_source_id 추적을 같은 원본으로
# 묶기 위한 checkpoint다. 파일명 mismatch를 조용히 통과시키는 대안은 운영
# smoke에서 CD tooltip/시장정의 원인을 역추적하기 어렵게 하므로 기각했다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
CD_SPECIFIC_ROWS_TO_VALIDATE = (
    14,
    15,
    17,
    18,
    19,
    54,
    55,
    56,
    57,
)

CD_MARKET_COLUMNS = (
    "cd_id",
    "name",
    "ml_id",
    "cd_filter_id",
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
    "source_file_version",
    "ingested_at",
)

CD_MARKET_SCHEMA = pa.schema(
    [
        pa.field("cd_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("cd_filter_id", pa.string(), nullable=False),
        pa.field("data_source", pa.string(), nullable=False),
        pa.field("analyze_class", pa.bool_(), nullable=False),
        pa.field("analyze_molecule", pa.bool_(), nullable=False),
        pa.field("analyze_dosage_form", pa.bool_(), nullable=False),
        pa.field("analyze_strength_pack", pa.bool_(), nullable=False),
        pa.field("analyze_nhi_type", pa.bool_(), nullable=False),
        pa.field("analyze_ox_gx", pa.bool_(), nullable=False),
        pa.field("analyze_fish_oil", pa.bool_(), nullable=False),
        pa.field("target_iqvia_1", pa.string(), nullable=True),
        pa.field("target_iqvia_2", pa.string(), nullable=True),
        pa.field("target_iqvia_3", pa.string(), nullable=True),
        pa.field("target_ubist_1", pa.string(), nullable=True),
        pa.field("target_ubist_2", pa.string(), nullable=True),
        pa.field("target_ubist_3", pa.string(), nullable=True),
        pa.field("target_ubist_4", pa.string(), nullable=True),
        pa.field("source_file_version", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us"), nullable=False),
    ]
)

CD_SPECS: tuple[dict[str, Any], ...] = default_mi_master_registry().cd_specs
EXPECTED_ROW_COUNT = len(CD_SPECS)
EXPECTED_DATA_SOURCE_COUNTS = dict(
    Counter(
        default_mi_master_registry()
        .market_by_id[str(spec["strategic_market_id"])]["source_type"]
        .lower()
        for spec in CD_SPECS
    )
)
COLLAPSE_PAIR_CD_ID = next(
    str(spec["cd_id"])
    for spec in CD_SPECS
    if len(tuple(spec["column_ids"])) > 1
)
EXPECTED_CD_IDS = tuple(str(spec["cd_id"]) for spec in CD_SPECS)

def validate_records(
    records: list[dict[str, Any]],
    ml_rows: list[dict[str, Any]],
    cd_filter_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"cd_market row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != CD_MARKET_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={CD_MARKET_COLUMNS}, actual={tuple(record.keys())}"
            )
    cd_ids = [str(record["cd_id"]) for record in records]
    if tuple(cd_ids) != EXPECTED_CD_IDS:
        raise ValueError(f"cd_id sequence mismatch: actual={cd_ids}")
    if len(set(cd_ids)) != EXPECTED_ROW_COUNT:
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
