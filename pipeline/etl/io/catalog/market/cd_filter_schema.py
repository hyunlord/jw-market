from __future__ import annotations

from pathlib import Path

import pyarrow as pa

DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_OUTPUT_FILE = Path("parquet/cd_filter/cd_filter.parquet")

# cd_filter는 CD narrower universe를 결정하는 핵심 입력이다.
# 260518 migration 뒤에는 ML과 CD가 같은 원본 버전에서 갈라져야 하므로,
# source_file_version을 여기서도 강제한다. CD filter만 과거 파일을 허용하는
# 방식은 rank/market size drift를 만든 경험이 있어 기각했다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_CD_FILTER_IDS = tuple(f"cdf_{index:03d}" for index in range(1, 20))
ML_EQUALS_CD_FILTER_IDS = {"cdf_004", "cdf_006", "cdf_007", "cdf_014", "cdf_016", "cdf_017"}
JSON_ARRAY_COLUMNS = ("atc3", "atc4", "molecule", "class")
FILTER_COLUMNS = ("atc3", "atc4", "molecule", "class", "nhi", "dosage_form")

CD_FILTER_COLUMNS = (
    "cd_filter_id",
    "name",
    "atc3",
    "atc4",
    "molecule",
    "class",
    "nhi",
    "dosage_form",
    "source_file_version",
    "ingested_at",
)

CD_FILTER_SCHEMA = pa.schema(
    [
        pa.field("cd_filter_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("atc3", pa.string(), nullable=True),
        pa.field("atc4", pa.string(), nullable=True),
        pa.field("molecule", pa.string(), nullable=True),
        pa.field("class", pa.string(), nullable=True),
        pa.field("nhi", pa.string(), nullable=True),
        pa.field("dosage_form", pa.string(), nullable=True),
        pa.field("source_file_version", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us"), nullable=False),
    ]
)

import json
import unicodedata
from datetime import datetime
from typing import Any

from pipeline.etl.io.catalog.market.cd_filter_specs import dumps_json_array
from pipeline.etl.io.catalog._lib.catalog_text import clean_text

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
