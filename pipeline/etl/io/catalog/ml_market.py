"""
prototype_17_ml_market_to_parquet.py
====================================
Phase 14 Step 14-2 ml_market -> Parquet.

Inputs:
- parquet/master_market_definition/master_market_definition.parquet
- parquet/master_drug/master_drug.parquet
- parquet/ml_market/ml_market.parquet fallback after Phase 14 cleanup

Output:
- parquet/ml_market/ml_market.parquet

Policy:
- Phase 14 replaces the Phase 11/12 market dimension set with a compact
  strategic-view schema.
- ml_market has one row per MI Master detail sheet (16 rows).
- Physical parquet types are typed: string, bool, and timestamp.
- D-46: analyze_* columns are a manual mapping matrix. The previous
  derivation from market definition markings/detail sheet column detection was
  retired after Phase 14 Step 14-9-1. Users update ANALYZE_MATRIX when a market
  is added or the intended analysis axes change.
- D-45: IQVIA target slots are fixed defaults for IQVIA/BOTH markets:
  KHPA/KCPA/KPA regardless of raw R54-R57.
- Q-57: BOTH-market UBIST target slots keep only UBIST specialty tokens
  (GH ... / CL ...). Raw notes such as IQVIA/KHPA or Ubist/... stay NULL.
"""

from __future__ import annotations

import argparse
import json
import re
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


DEFAULT_MARKET_DEFINITION_FILE = Path(
    "parquet/master_market_definition/master_market_definition.parquet"
)
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/ml_market/ml_market.parquet")

# ml_market는 전략뷰 Market Landscape의 envelope 정의다.
# 이번 rebuild의 기준은 260518 MI Master이므로, 여기서도 같은 파일명을
# source_file_version으로 요구한다. market metadata만 최신으로 바꾸고
# ml_market parquet가 4/22이면 원인분석/시장현황이 다른 시장정의를 보게 되어
# 기각한다.
EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (원본파일 점검용 재공유 2026.05.18).xlsx"
EXPECTED_MARKET_IDS = tuple(f"strategy_{index:03d}" for index in range(1, 17))
EXPECTED_ML_IDS = tuple(f"ml_{index:03d}" for index in range(1, 17))
EXPECTED_DATA_SOURCE_COUNTS = {"iqvia": 8, "ubist": 6, "both": 2}
EXPECTED_STRATEGY_005_SOURCE = "ubist"

ANALYZE_COLUMNS = (
    "analyze_class",
    "analyze_molecule",
    "analyze_dosage_form",
    "analyze_strength_pack",
    "analyze_nhi_type",
    "analyze_ox_gx",
    "analyze_fish_oil",
)

# D-46 manual analyze matrix.
#
# These seven booleans are not derived from R14-R18 markers or detail-sheet
# header existence anymore. Phase 14 Step 14-9-1 found that raw markings and
# materialized strategic fields diverge in several markets, so the intended
# analysis axes are now an explicit staging dictionary. Keep every ml_id present
# and update this matrix directly when business intent changes.
ANALYZE_MATRIX: dict[str, dict[str, bool]] = {
    "ml_001": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_002": {
        "class": True,
        "molecule": True,
        "dosage_form": True,
        "strength_pack": False,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_003": {
        "class": True,
        "molecule": True,
        "dosage_form": True,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_004": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": True,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_005": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_006": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": True,
        "nhi_type": False,
        "ox_gx": True,
        "fish_oil": False,
    },
    "ml_007": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_008": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_009": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_010": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_011": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": False,
        "ox_gx": True,
        "fish_oil": False,
    },
    "ml_012": {
        "class": True,
        "molecule": True,
        "dosage_form": True,
        "strength_pack": True,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_013": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": False,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_014": {
        "class": True,
        "molecule": True,
        "dosage_form": True,
        "strength_pack": True,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": True,
    },
    "ml_015": {
        "class": False,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": True,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": False,
    },
    "ml_016": {
        "class": True,
        "molecule": True,
        "dosage_form": False,
        "strength_pack": True,
        "nhi_type": True,
        "ox_gx": False,
        "fish_oil": False,
    },
}
ML_MARKET_COLUMNS = (
    "ml_id",
    "name",
    "data_source",
    "atc_codes_json",
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

ML_MARKET_SCHEMA = pa.schema(
    [
        pa.field("ml_id", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("data_source", pa.string(), nullable=False),
        pa.field("atc_codes_json", pa.string(), nullable=False),
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

AUDIT_CODES = ("KHPA", "KCPA", "KPA")
UBIST_TARGET_PATTERN = re.compile(r"^(GH|CL)\s+\S+", re.IGNORECASE)


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def parse_json_text(value: Any, fallback: Any) -> Any:
    text = clean_text(value)
    if text is None:
        return fallback
    return json.loads(text)


def normalize_data_source(value: Any, strategic_market_id: str) -> str:
    text = clean_text(value)
    if text is None:
        raise ValueError(f"{strategic_market_id} source_type is empty")
    lowered = text.lower()
    if lowered not in {"iqvia", "ubist", "both"}:
        raise ValueError(f"{strategic_market_id} invalid source_type={text!r}")
    return lowered


def normalize_market_name(value: Any) -> str:
    text = clean_text(value)
    if text is None:
        raise ValueError("market_name is empty")
    return text.replace("위너프A+", "위너프에이플러스")


def analyze_values_for_ml(ml_id: str) -> dict[str, bool]:
    matrix = ANALYZE_MATRIX.get(ml_id)
    if matrix is None:
        raise ValueError(f"ANALYZE_MATRIX missing ml_id={ml_id}")
    return {f"analyze_{key}": bool(value) for key, value in matrix.items()}


def target_values_by_row(raw_row_json: str) -> dict[int, list[str]]:
    payload = parse_json_text(raw_row_json, {})
    by_row: dict[int, list[str]] = {54: [], 55: [], 56: [], 57: []}
    for column in payload.get("columns", []):
        for item in column.get("values", []):
            row_id = int(item.get("row_id"))
            if row_id not in by_row:
                continue
            text = clean_text(item.get("value"))
            if text is not None and text not in by_row[row_id]:
                by_row[row_id].append(text)
    return by_row


def join_unique(values: list[str]) -> str | None:
    if not values:
        return None
    return " / ".join(values)


def is_ubist_target_token(value: str | None) -> bool:
    return value is not None and UBIST_TARGET_PATTERN.search(value) is not None


def ubist_target_from_values(values: list[str]) -> str | None:
    return join_unique([value for value in values if is_ubist_target_token(value)])


def apply_target_policy(record: dict[str, Any], raw_targets: dict[int, list[str]]) -> None:
    """Apply Phase 14 Step 14-8 target policy in-place."""
    data_source = str(record["data_source"])

    if data_source in {"iqvia", "both"}:
        audit_codes: list[str | None] = list(AUDIT_CODES)
    else:
        audit_codes = [None, None, None]

    if data_source == "ubist":
        ubist_targets = [join_unique(raw_targets[row_id]) for row_id in (54, 55, 56, 57)]
    elif data_source == "both":
        ubist_targets = [
            ubist_target_from_values(raw_targets[row_id])
            for row_id in (54, 55, 56, 57)
        ]
    else:
        ubist_targets = [None, None, None, None]

    record.update(
        {
            "target_iqvia_1": audit_codes[0],
            "target_iqvia_2": audit_codes[1],
            "target_iqvia_3": audit_codes[2],
            "target_ubist_1": ubist_targets[0],
            "target_ubist_2": ubist_targets[1],
            "target_ubist_3": ubist_targets[2],
            "target_ubist_4": ubist_targets[3],
        }
    )


def make_record(
    ordinal: int,
    market_definition_row: dict[str, Any],
    master_drug_rows: list[dict[str, Any]],
    ingested_at: datetime,
) -> dict[str, Any]:
    strategic_market_id = str(market_definition_row["strategic_market_id"])
    ml_id = f"ml_{ordinal:03d}"
    data_source = normalize_data_source(
        market_definition_row.get("source_type"),
        strategic_market_id,
    )
    raw_targets = target_values_by_row(str(market_definition_row["raw_row_json"]))

    record: dict[str, Any] = {
        "ml_id": ml_id,
        "name": normalize_market_name(market_definition_row.get("market_name")),
        "data_source": data_source,
        "atc_codes_json": clean_text(market_definition_row.get("market_atc_codes_json")) or "[]",
        "source_file_version": clean_text(market_definition_row.get("source_file_version")),
        "ingested_at": ingested_at,
    }
    record.update(analyze_values_for_ml(ml_id))

    apply_target_policy(record, raw_targets)
    return {column: record.get(column) for column in ML_MARKET_COLUMNS}


def _source_file_version(rows: list[dict[str, Any]]) -> str:
    versions = {
        clean_text(row.get("source_file_version"))
        for row in rows
        if clean_text(row.get("source_file_version")) is not None
    }
    if versions != {unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION)}:
        raise ValueError(
            f"source_file_version mismatch: expected={EXPECTED_SOURCE_FILE_VERSION!r}, "
            f"actual={sorted(v for v in versions if v)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def load_existing_ml_market_records(
    existing_path: Path,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rows = read_parquet_rows(existing_path)
    _source_file_version(rows)
    timestamp = ingested_at or utc_now_datetime()
    records: list[dict[str, Any]] = []
    for row in rows:
        record = {column: row.get(column) for column in ML_MARKET_COLUMNS}
        record["atc_codes_json"] = clean_text(record.get("atc_codes_json")) or "[]"
        record["ingested_at"] = timestamp
        raw_targets = {row_id: [] for row_id in (54, 55, 56, 57)}
        # Existing Phase 14 rows already contain normalized UBIST target slots.
        # D-45/Q-57 only need data_source-aware rewriting at this fallback stage.
        if str(record["data_source"]) == "ubist":
            raw_targets = {
                54: [clean_text(record.get("target_ubist_1"))] if clean_text(record.get("target_ubist_1")) else [],
                55: [clean_text(record.get("target_ubist_2"))] if clean_text(record.get("target_ubist_2")) else [],
                56: [clean_text(record.get("target_ubist_3"))] if clean_text(record.get("target_ubist_3")) else [],
                57: [clean_text(record.get("target_ubist_4"))] if clean_text(record.get("target_ubist_4")) else [],
            }
        elif str(record["data_source"]) == "both" and record["ml_id"] != "ml_015":
            raw_targets = {
                54: [clean_text(record.get("target_ubist_1"))] if clean_text(record.get("target_ubist_1")) else [],
                55: [clean_text(record.get("target_ubist_2"))] if clean_text(record.get("target_ubist_2")) else [],
                56: [clean_text(record.get("target_ubist_3"))] if clean_text(record.get("target_ubist_3")) else [],
                57: [clean_text(record.get("target_ubist_4"))] if clean_text(record.get("target_ubist_4")) else [],
            }
        record.update(analyze_values_for_ml(str(record["ml_id"])))
        apply_target_policy(record, raw_targets)
        records.append({column: record.get(column) for column in ML_MARKET_COLUMNS})
    validate_records(records)
    return records


def load_ml_market_records(
    market_definition_path: Path,
    master_drug_path: Path,
    existing_path: Path = DEFAULT_OUTPUT_FILE,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    if not market_definition_path.exists() or not master_drug_path.exists():
        if not existing_path.exists():
            raise FileNotFoundError(
                "Phase 14 source parquet files are missing and existing ml_market "
                f"fallback was not found: {existing_path}"
            )
        return load_existing_ml_market_records(existing_path, ingested_at)

    market_definition_rows = read_parquet_rows(market_definition_path)
    master_drug_rows = read_parquet_rows(master_drug_path)
    _source_file_version(market_definition_rows)
    _source_file_version(master_drug_rows)

    market_definition_by_id = {
        str(row.get("strategic_market_id")): row for row in market_definition_rows
    }
    actual_ids = set(market_definition_by_id)
    expected_ids = set(EXPECTED_MARKET_IDS)
    if actual_ids != expected_ids:
        raise ValueError(
            f"market_definition strategic_market_id mismatch: "
            f"missing={sorted(expected_ids - actual_ids)}, extra={sorted(actual_ids - expected_ids)}"
        )

    timestamp = ingested_at or utc_now_datetime()
    records = [
        make_record(
            ordinal=index,
            market_definition_row=market_definition_by_id[strategic_market_id],
            master_drug_rows=master_drug_rows,
            ingested_at=timestamp,
        )
        for index, strategic_market_id in enumerate(EXPECTED_MARKET_IDS, start=1)
    ]
    validate_records(records)
    return records


def validate_records(records: list[dict[str, Any]]) -> None:
    if len(records) != 16:
        raise ValueError(f"ml_market row count must be 16, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != ML_MARKET_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: "
                f"expected={ML_MARKET_COLUMNS}, actual={tuple(record.keys())}"
            )

    ml_ids = [str(record["ml_id"]) for record in records]
    if tuple(ml_ids) != EXPECTED_ML_IDS:
        raise ValueError(f"ml_id sequence mismatch: actual={ml_ids}")
    if len(set(ml_ids)) != 16:
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


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=ML_MARKET_SCHEMA)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != ML_MARKET_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={ML_MARKET_SCHEMA}\nactual={table.schema}")
    rows = table.to_pylist()
    validate_records(rows)


def _count_true(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if bool(record[column]))


def _nonnull_count(records: list[dict[str, Any]], column: str) -> int:
    return sum(1 for record in records if record.get(column) is not None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 14 ml_market parquet.")
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--existing", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_ml_market_records(args.market_definition, args.master_drug, args.existing)
    write_parquet(records, args.output)
    validate_written_parquet(args.output)

    print("prototype Phase 14 Step 14-2 ml_market -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(ML_MARKET_COLUMNS)}")
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
