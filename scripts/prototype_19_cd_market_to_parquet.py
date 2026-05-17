"""
prototype_19_cd_market_to_parquet.py
====================================
Phase 14 Step 14-4 cd_market -> Parquet.

Inputs:
- parquet/ml_market/ml_market.parquet
- parquet/cd_filter/cd_filter.parquet
- parquet/master_market_definition/master_market_definition.parquet
- parquet/cd_market/cd_market.parquet fallback after Phase 14 cleanup

Output:
- parquet/cd_market/cd_market.parquet

Policy:
- cd_market has one row per Competitive Dynamics unit (19 rows).
- cd_market inherits data_source, analysis booleans, and target fields from
  ml_market. D-46 makes analyze_* a manual matrix on ml_market, so cd_market
  must not apply CD-specific analyze overrides.
- Step 14-8: target_iqvia_* / target_ubist_* are inherited from the parent
  ml_market row after D-45 / Q-57 correction.
- Q/R 페린젝트/베노훼럼 collapse requires identical source values for the
  fields materialized here. A mismatch is a stop condition.
- Physical parquet types are typed: string, bool, and timestamp.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")


DEFAULT_ML_MARKET_FILE = Path("parquet/ml_market/ml_market.parquet")
DEFAULT_CD_FILTER_FILE = Path("parquet/cd_filter/cd_filter.parquet")
DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_OUTPUT_FILE = Path("parquet/cd_market/cd_market.parquet")

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx"
EXPECTED_CD_IDS = tuple(f"cd_{index:03d}" for index in range(1, 20))
EXPECTED_DATA_SOURCE_COUNTS = {"both": 2, "iqvia": 9, "ubist": 8}
COLLAPSE_PAIR_CD_ID = "cd_015"
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

CD_SPECS: tuple[dict[str, Any], ...] = (
    {"cd_id": "cd_001", "name": "라베칸 라베칸듀오", "ml_id": "ml_001", "cd_filter_id": "cdf_001", "strategic_market_id": "strategy_001", "column_ids": (3,)},
    {"cd_id": "cd_002", "name": "제이클", "ml_id": "ml_002", "cd_filter_id": "cdf_002", "strategic_market_id": "strategy_002", "column_ids": (4,)},
    {"cd_id": "cd_003", "name": "가드렛 가드메트", "ml_id": "ml_003", "cd_filter_id": "cdf_003", "strategic_market_id": "strategy_003", "column_ids": (5,)},
    {"cd_id": "cd_004", "name": "타발리스", "ml_id": "ml_004", "cd_filter_id": "cdf_004", "strategic_market_id": "strategy_004", "column_ids": (6,)},
    {"cd_id": "cd_005", "name": "시그마트", "ml_id": "ml_005", "cd_filter_id": "cdf_005", "strategic_market_id": "strategy_005", "column_ids": (7,)},
    {"cd_id": "cd_006", "name": "리바로 리바로젯", "ml_id": "ml_006", "cd_filter_id": "cdf_006", "strategic_market_id": "strategy_006", "column_ids": (8,)},
    {"cd_id": "cd_007", "name": "리바로페노", "ml_id": "ml_007", "cd_filter_id": "cdf_007", "strategic_market_id": "strategy_007", "column_ids": (9,)},
    {"cd_id": "cd_008", "name": "리바로하이", "ml_id": "ml_008", "cd_filter_id": "cdf_008", "strategic_market_id": "strategy_008", "column_ids": (10,)},
    {"cd_id": "cd_009", "name": "리바로브이", "ml_id": "ml_008", "cd_filter_id": "cdf_009", "strategic_market_id": "strategy_008", "column_ids": (11,)},
    {"cd_id": "cd_010", "name": "트루패스", "ml_id": "ml_009", "cd_filter_id": "cdf_010", "strategic_market_id": "strategy_009", "column_ids": (12,)},
    {"cd_id": "cd_011", "name": "피나스타 제이다트", "ml_id": "ml_009", "cd_filter_id": "cdf_011", "strategic_market_id": "strategy_009", "column_ids": (13,)},
    {"cd_id": "cd_012", "name": "뉴트로진", "ml_id": "ml_010", "cd_filter_id": "cdf_012", "strategic_market_id": "strategy_010", "column_ids": (14,)},
    {"cd_id": "cd_013", "name": "모빌리아", "ml_id": "ml_010", "cd_filter_id": "cdf_013", "strategic_market_id": "strategy_010", "column_ids": (15,)},
    {"cd_id": "cd_014", "name": "악템라", "ml_id": "ml_011", "cd_filter_id": "cdf_014", "strategic_market_id": "strategy_011", "column_ids": (16,)},
    {"cd_id": "cd_015", "name": "페린젝트 베노훼럼", "ml_id": "ml_012", "cd_filter_id": "cdf_015", "strategic_market_id": "strategy_012", "column_ids": (17, 18)},
    {"cd_id": "cd_016", "name": "헴리브라", "ml_id": "ml_013", "cd_filter_id": "cdf_016", "strategic_market_id": "strategy_013", "column_ids": (19,)},
    {"cd_id": "cd_017", "name": "엔커버", "ml_id": "ml_015", "cd_filter_id": "cdf_017", "strategic_market_id": "strategy_015", "column_ids": (20,)},
    {"cd_id": "cd_018", "name": "위너프 위너프에이플러스", "ml_id": "ml_014", "cd_filter_id": "cdf_018", "strategic_market_id": "strategy_014", "column_ids": (21,)},
    {"cd_id": "cd_019", "name": "플라주오피", "ml_id": "ml_016", "cd_filter_id": "cdf_019", "strategic_market_id": "strategy_016", "column_ids": (22,)},
)


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    return text.replace("위너프A+", "위너프에이플러스")


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def source_file_version(rows: list[dict[str, Any]], label: str) -> str:
    versions = {
        clean_text(row.get("source_file_version"))
        for row in rows
        if clean_text(row.get("source_file_version")) is not None
    }
    if versions != {EXPECTED_SOURCE_FILE_VERSION}:
        raise ValueError(
            f"{label} source_file_version mismatch: "
            f"expected={EXPECTED_SOURCE_FILE_VERSION!r}, actual={sorted(v for v in versions if v)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def raw_column_by_id(market_definition_row: dict[str, Any]) -> dict[int, dict[str, Any]]:
    payload = json.loads(str(market_definition_row["raw_row_json"]))
    return {int(column["column_id"]): column for column in payload.get("columns", [])}


def raw_value_for(
    market_definition_row: dict[str, Any],
    column_id: int,
    row_id: int,
) -> str | None:
    column = raw_column_by_id(market_definition_row).get(column_id)
    if column is None:
        raise ValueError(
            f"{market_definition_row['strategic_market_id']} missing raw column_id={column_id}"
        )
    for item in column.get("values", []):
        if int(item.get("row_id")) == row_id:
            return clean_text(item.get("value"))
    return None


def cd_specific_value(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
    row_id: int,
) -> str | None:
    values = [
        raw_value_for(market_definition_row, int(column_id), row_id)
        for column_id in tuple(spec["column_ids"])
    ]
    non_empty = [value for value in values if value is not None]
    if not non_empty:
        return None
    unique_values = []
    for value in non_empty:
        if value not in unique_values:
            unique_values.append(value)
    if spec["cd_id"] == COLLAPSE_PAIR_CD_ID and len(unique_values) > 1:
        raise ValueError(
            f"{COLLAPSE_PAIR_CD_ID} collapse pair raw mismatch at row {row_id}: {values}"
        )
    return unique_values[0]


def target_iqvia_value(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
    ml_row: dict[str, Any],
    target_index: int,
) -> str | None:
    return clean_text(ml_row.get(f"target_iqvia_{target_index}"))


def target_ubist_value(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
    ml_row: dict[str, Any],
    target_index: int,
) -> str | None:
    return clean_text(ml_row.get(f"target_ubist_{target_index}"))


def make_record(
    spec: dict[str, Any],
    ml_by_id: dict[str, dict[str, Any]],
    market_definition_by_smid: dict[str, dict[str, Any]],
    ingested_at: datetime,
) -> dict[str, Any]:
    ml_row = ml_by_id[str(spec["ml_id"])]
    market_definition_row = market_definition_by_smid[str(spec["strategic_market_id"])]

    record: dict[str, Any] = {
        "cd_id": str(spec["cd_id"]),
        "name": str(spec["name"]),
        "ml_id": str(spec["ml_id"]),
        "cd_filter_id": str(spec["cd_filter_id"]),
        "data_source": str(ml_row["data_source"]),
        "source_file_version": EXPECTED_SOURCE_FILE_VERSION,
        "ingested_at": ingested_at,
    }
    for column in (
        "analyze_class",
        "analyze_molecule",
        "analyze_dosage_form",
        "analyze_strength_pack",
        "analyze_nhi_type",
        "analyze_ox_gx",
        "analyze_fish_oil",
    ):
        record[column] = bool(ml_row[column])
    for index in range(1, 4):
        record[f"target_iqvia_{index}"] = target_iqvia_value(
            spec, market_definition_row, ml_row, index
        )
    for index in range(1, 5):
        record[f"target_ubist_{index}"] = target_ubist_value(
            spec, market_definition_row, ml_row, index
        )
    return {column: record.get(column) for column in CD_MARKET_COLUMNS}


def validate_collapse_pair_raw(
    spec: dict[str, Any],
    market_definition_row: dict[str, Any],
) -> None:
    if spec["cd_id"] != COLLAPSE_PAIR_CD_ID:
        return
    for row_id in CD_SPECIFIC_ROWS_TO_VALIDATE:
        cd_specific_value(spec, market_definition_row, row_id)


def load_cd_market_records(
    ml_market_path: Path,
    cd_filter_path: Path,
    market_definition_path: Path,
    existing_path: Path = DEFAULT_OUTPUT_FILE,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    ml_rows = read_parquet_rows(ml_market_path)
    cd_filter_rows = read_parquet_rows(cd_filter_path)
    if not market_definition_path.exists():
        if not existing_path.exists():
            raise FileNotFoundError(
                "Phase 14 source market_definition parquet is missing and existing "
                f"cd_market fallback was not found: {existing_path}"
            )
        return load_existing_cd_market_records(
            existing_path=existing_path,
            ml_rows=ml_rows,
            cd_filter_rows=cd_filter_rows,
            ingested_at=ingested_at,
        )

    market_definition_rows = read_parquet_rows(market_definition_path)
    source_file_version(ml_rows, "ml_market")
    source_file_version(cd_filter_rows, "cd_filter")
    source_file_version(market_definition_rows, "master_market_definition")

    ml_by_id = {str(row["ml_id"]): row for row in ml_rows}
    market_definition_by_smid = {
        str(row["strategic_market_id"]): row for row in market_definition_rows
    }

    for spec in CD_SPECS:
        validate_collapse_pair_raw(
            spec,
            market_definition_by_smid[str(spec["strategic_market_id"])],
        )

    timestamp = ingested_at or utc_now_datetime()
    records = [
        make_record(spec, ml_by_id, market_definition_by_smid, timestamp)
        for spec in CD_SPECS
    ]
    validate_records(records, ml_rows, cd_filter_rows)
    return records


def load_existing_cd_market_records(
    existing_path: Path,
    ml_rows: list[dict[str, Any]],
    cd_filter_rows: list[dict[str, Any]],
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = read_parquet_rows(existing_path)
    source_file_version(ml_rows, "ml_market")
    source_file_version(cd_filter_rows, "cd_filter")
    source_file_version(rows, "cd_market")
    ml_by_id = {str(row["ml_id"]): row for row in ml_rows}
    timestamp = ingested_at or utc_now_datetime()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = {column: row.get(column) for column in CD_MARKET_COLUMNS}
        ml_row = ml_by_id[str(record["ml_id"])]
        record["data_source"] = str(ml_row["data_source"])
        for column in (
            "analyze_class",
            "analyze_molecule",
            "analyze_dosage_form",
            "analyze_strength_pack",
            "analyze_nhi_type",
            "analyze_ox_gx",
            "analyze_fish_oil",
        ):
            record[column] = bool(ml_row[column])
        for index in range(1, 4):
            record[f"target_iqvia_{index}"] = clean_text(
                ml_row.get(f"target_iqvia_{index}")
            )
        for index in range(1, 5):
            record[f"target_ubist_{index}"] = clean_text(
                ml_row.get(f"target_ubist_{index}")
            )
        record["source_file_version"] = EXPECTED_SOURCE_FILE_VERSION
        record["ingested_at"] = timestamp
        records.append({column: record.get(column) for column in CD_MARKET_COLUMNS})
    validate_records(records, ml_rows, cd_filter_rows)
    return records


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
        if clean_text(record["source_file_version"]) != EXPECTED_SOURCE_FILE_VERSION:
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


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=CD_MARKET_SCHEMA)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != CD_MARKET_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={CD_MARKET_SCHEMA}\nactual={table.schema}")
    records = table.to_pylist()
    ml_rows = read_parquet_rows(DEFAULT_ML_MARKET_FILE)
    cd_filter_rows = read_parquet_rows(DEFAULT_CD_FILTER_FILE)
    validate_records(records, ml_rows, cd_filter_rows)


def _count_true(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if bool(record[column]))


def _nonnull_count(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if record.get(column) is not None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 14 cd_market parquet.")
    parser.add_argument("--ml-market", type=Path, default=DEFAULT_ML_MARKET_FILE)
    parser.add_argument("--cd-filter", type=Path, default=DEFAULT_CD_FILTER_FILE)
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--existing", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_cd_market_records(
        args.ml_market,
        args.cd_filter,
        args.market_definition,
        args.existing,
    )
    write_parquet(records, args.output)
    validate_written_parquet(args.output)

    print("prototype Phase 14 Step 14-4 cd_market -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(CD_MARKET_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at'].isoformat(sep=' ', timespec='seconds')}")
    print("data_source_distribution:")
    for source, count in sorted(Counter(record["data_source"] for record in records).items()):
        print(f"  {source}: {count}")
    print("analyze_true_counts:")
    for column in (
        "analyze_class",
        "analyze_molecule",
        "analyze_dosage_form",
        "analyze_strength_pack",
        "analyze_nhi_type",
        "analyze_ox_gx",
        "analyze_fish_oil",
    ):
        print(f"  {column}: {_count_true(records, column)}")
    print("target_nonnull_counts:")
    for column in (
        "target_iqvia_1",
        "target_iqvia_2",
        "target_iqvia_3",
        "target_ubist_1",
        "target_ubist_2",
        "target_ubist_3",
        "target_ubist_4",
    ):
        print(f"  {column}: {_nonnull_count(records, column)}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
