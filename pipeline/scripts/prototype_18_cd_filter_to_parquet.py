"""
prototype_18_cd_filter_to_parquet.py
====================================
Phase 14 Step 14-3 cd_filter -> Parquet.

Inputs:
- parquet/master_market_definition/master_market_definition.parquet

Output:
- parquet/cd_filter/cd_filter.parquet

Policy:
- cd_filter is the normalized condition dictionary for Competitive Dynamics.
- The grain is one row per CD unit: 20 product columns collapsed to 19 units
  with 페린젝트/베노훼럼 Q/R collapsed.
- Multi-value condition columns are JSON array strings, including single values.
- Sheet-total / ML==CD filters have all condition columns NULL.
- Physical parquet types are typed: string and timestamp.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")


DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_OUTPUT_FILE = Path("parquet/cd_filter/cd_filter.parquet")

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx"
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


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def dumps_json_array(values: list[str] | None) -> str | None:
    if values is None:
        return None
    cleaned = [clean_text(value) for value in values]
    if any(value is None for value in cleaned):
        raise ValueError(f"JSON array values must be non-empty strings: {values!r}")
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def source_file_version(path: Path) -> str:
    rows = read_parquet_rows(path)
    versions = {
        clean_text(row.get("source_file_version"))
        for row in rows
        if clean_text(row.get("source_file_version")) is not None
    }
    if versions != {EXPECTED_SOURCE_FILE_VERSION}:
        raise ValueError(
            f"source_file_version mismatch: expected={EXPECTED_SOURCE_FILE_VERSION!r}, "
            f"actual={sorted(v for v in versions if v)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def raw_filter_records(source_file_version_value: str, ingested_at: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "cd_filter_id": "cdf_001",
            "name": "라베칸 라베칸듀오",
            "atc3": None,
            "atc4": None,
            "molecule": dumps_json_array(["Rabeprazole"]),
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_002",
            "name": "제이클",
            "atc3": None,
            "atc4": dumps_json_array(["A06B1", "A06B2"]),
            "molecule": None,
            "class": None,
            "nhi": "NON_NHI",
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_003",
            "name": "가드렛 가드메트",
            "atc3": None,
            "atc4": dumps_json_array(["A10N3", "A10N1"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_004",
            "name": "타발리스",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_005",
            "name": "시그마트",
            "atc3": dumps_json_array(["C1D"]),
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_006",
            "name": "리바로 리바로젯",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_007",
            "name": "리바로페노",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_008",
            "name": "리바로하이",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": dumps_json_array(["Statin/ARB/CCB"]),
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_009",
            "name": "리바로브이",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": dumps_json_array(["Statin/ARB"]),
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_010",
            "name": "트루패스",
            "atc3": None,
            "atc4": dumps_json_array(["G4C2"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_011",
            "name": "피나스타 제이다트",
            "atc3": None,
            "atc4": dumps_json_array(["G4C3"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_012",
            "name": "뉴트로진",
            "atc3": None,
            "atc4": dumps_json_array(["L03A1"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_013",
            "name": "모빌리아",
            "atc3": None,
            "atc4": dumps_json_array(["L03A9"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_014",
            "name": "악템라",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_015",
            "name": "페린젝트 베노훼럼",
            "atc3": None,
            "atc4": dumps_json_array(["B03A1"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": "IV Iron",
        },
        {
            "cd_filter_id": "cdf_016",
            "name": "헴리브라",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_017",
            "name": "엔커버",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_018",
            "name": "위너프 위너프에이플러스",
            "atc3": None,
            "atc4": dumps_json_array(["K01D2"]),
            "molecule": None,
            "class": dumps_json_array(["3CB"]),
            "nhi": "급여",
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_019",
            "name": "플라주오피",
            "atc3": None,
            "atc4": dumps_json_array(["K01A1", "K01A3"]),
            "molecule": None,
            "class": dumps_json_array(["Acetated"]),
            "nhi": None,
            "dosage_form": None,
        },
    ]

    return [
        {
            **row,
            "source_file_version": source_file_version_value,
            "ingested_at": ingested_at,
        }
        for row in rows
    ]


def load_cd_filter_records(
    market_definition_path: Path,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    version = source_file_version(market_definition_path)
    timestamp = ingested_at or utc_now_datetime()
    records = raw_filter_records(version, timestamp)
    validate_records(records)
    return records


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
        if clean_text(record["source_file_version"]) != EXPECTED_SOURCE_FILE_VERSION:
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


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=CD_FILTER_SCHEMA)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != CD_FILTER_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={CD_FILTER_SCHEMA}\nactual={table.schema}")
    validate_records(table.to_pylist())


def count_non_null(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if record[column] is not None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 14 cd_filter parquet.")
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_cd_filter_records(args.market_definition)
    write_parquet(records, args.output)
    validate_written_parquet(args.output)

    print("prototype Phase 14 Step 14-3 cd_filter -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(CD_FILTER_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at'].isoformat(sep=' ', timespec='seconds')}")
    print("filter_non_null_counts:")
    for column in FILTER_COLUMNS:
        print(f"  {column}: {count_non_null(records, column)}")
    print("ml_equals_cd_filter_ids:")
    for filter_id in sorted(ML_EQUALS_CD_FILTER_IDS):
        print(f"  {filter_id}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
