"""
verify_master_market_definition_parquet.py
==========================================
Verify Phase 09a MI Master market_definition Parquet against the raw workbook.

Small-data policy:
- 16 rows total, so Phase 2 compares every row, not samples.
- Expected rows are regenerated from the same canonical Phase 09a extraction
  logic used by prototype_07_master_market_definition_to_parquet.py.
- ingested_at is checked for format only, not value equality.

Usage:
    python3 scripts/verify_master_market_definition_parquet.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from prototype_07_master_market_definition_to_parquet import (  # noqa: E402
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_STRATEGIC_MARKET_IDS,
    MARKET_BY_ID,
    MARKET_DEFINITION_COLUMNS,
    MASTER_MARKET_DEFINITION_COLUMNS,
    iter_market_definition_rows,
)


JSON_COLUMNS = (
    "full_market_atc4_codes_json",
    "direct_competition_brands_json",
    "analysis_levels_json",
    "target_customer_priority_json",
    "raw_row_json",
)

FOUR_STRUCTURED_JSON_COLUMNS = (
    "full_market_atc4_codes_json",
    "direct_competition_brands_json",
    "analysis_levels_json",
    "target_customer_priority_json",
)

EXPECTED_ANALYSIS_LEVEL_KEYS = {
    "Brand",
    "Class",
    "Dosage Form",
    "Etc",
    "Metrics",
    "Molecule",
    "Strength",
}

INGESTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def load_parquet_rows(parquet_file: Path) -> list[dict[str, Any]]:
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    table = pq.read_table(parquet_file)
    return table.to_pylist()


def load_expected_rows(input_file: Path) -> list[dict[str, Any]]:
    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")
    return list(iter_market_definition_rows(input_file, ingested_at="__VERIFY_INGESTED_AT__"))


def json_size(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if "columns" in value and isinstance(value["columns"], list):
            return sum(len(column.get("values", [])) for column in value["columns"])
        return len(value)
    return 0


def summarize_sizes(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for column in JSON_COLUMNS:
        sizes = [json_size(json.loads(row[column])) for row in rows]
        summary[column] = {
            "min": min(sizes),
            "max": max(sizes),
            "distribution": dict(sorted(Counter(sizes).items())),
        }
    return summary


def phase0_full_count(expected_rows: list[dict[str, Any]], parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 0] 전수 row count 검증")
    print("=" * 72)
    raw_n = len(expected_rows)
    parquet_n = len(parquet_rows)
    ok = raw_n == parquet_n == 16
    print(f"  raw expected rows: {raw_n}")
    print(f"  parquet rows:      {parquet_n}")
    print(f"  required rows:     16")
    print(f"  result:            {'PASS' if ok else 'FAIL'}")
    return (1, 0) if ok else (0, 1)


def phase1_stats(parquet_rows: list[dict[str, Any]]) -> dict[str, Any]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)

    source_type_counts = Counter(row["source_type"] for row in parquet_rows)
    print("  strategic_market_id list:")
    for index, row in enumerate(parquet_rows, start=1):
        print(
            f"    {index:02d}. {row['strategic_market_id']} | "
            f"{row['market_name']} | {row['source_type']}"
        )

    print("\n  source_type distribution:")
    for source_type, count in sorted(source_type_counts.items()):
        print(f"    {source_type}: {count}")

    print("\n  JSON sample size distribution:")
    size_summary = summarize_sizes(parquet_rows)
    for column, stat in size_summary.items():
        print(
            f"    {column}: min={stat['min']} max={stat['max']} "
            f"distribution={stat['distribution']}"
        )

    return {
        "source_type_counts": dict(source_type_counts),
        "json_size_summary": size_summary,
    }


def compare_json(left: str, right: str) -> bool:
    return json.loads(left) == json.loads(right)


def phase2_full_row_compare(
    expected_rows: list[dict[str, Any]],
    parquet_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 2] 16 row 전수 raw 비교")
    print("=" * 72)

    expected_by_id = {row["strategic_market_id"]: row for row in expected_rows}
    pass_count = 0
    fail_count = 0

    for index, actual in enumerate(parquet_rows, start=1):
        strategic_market_id = actual["strategic_market_id"]
        expected = expected_by_id.get(strategic_market_id)
        title = f"{strategic_market_id} | {actual.get('market_name')}"
        if expected is None:
            print(f"  [{index:02d}] FAIL missing expected row: {title}")
            fail_count += 1
            continue

        mismatches = []
        for column in MASTER_MARKET_DEFINITION_COLUMNS:
            if column == "ingested_at":
                continue
            if column in JSON_COLUMNS:
                if not compare_json(actual[column], expected[column]):
                    mismatches.append(column)
            elif actual.get(column) != expected.get(column):
                mismatches.append(column)

        config = MARKET_BY_ID[strategic_market_id]
        if actual["market_name"] != config["sheet_name"]:
            mismatches.append("market_name(config)")

        expected_description = (
            "IQVIA 기준 하모닐란과 엔커버 2개의 PRODUCT NAME KOR 에 대해 PACK DESC 를 하위분류로 4가지로 분석"
            if strategic_market_id == "strategy_015"
            else None
        )
        if actual["description"] != expected_description:
            mismatches.append("description")

        expected_funnel = "O" if strategic_market_id == "strategy_001" else None
        if actual["analysis_level_funnel"] != expected_funnel:
            mismatches.append("analysis_level_funnel")

        levels = json.loads(actual["analysis_levels_json"])
        etc_join = "; ".join(str(value).strip() for value in levels["Etc"]["values"] if value)
        if actual["analysis_level_etc"] != etc_join:
            mismatches.append("analysis_level_etc")

        if mismatches:
            print(f"  [{index:02d}] FAIL {title}: {mismatches}")
            fail_count += 1
        else:
            print(f"  [{index:02d}] PASS {title}")
            pass_count += 1

    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def phase3_json_validity(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 3] JSON validity / shape")
    print("=" * 72)

    pass_count = 0
    fail_count = 0
    for row in parquet_rows:
        strategic_market_id = row["strategic_market_id"]
        row_errors = []

        parsed = {}
        for column in JSON_COLUMNS:
            try:
                parsed[column] = json.loads(row[column])
            except Exception as exc:  # noqa: BLE001
                row_errors.append(f"{column}: invalid JSON ({exc})")

        if "analysis_levels_json" in parsed:
            keys = set(parsed["analysis_levels_json"].keys())
            if keys != EXPECTED_ANALYSIS_LEVEL_KEYS:
                row_errors.append(f"analysis_levels_json keys={sorted(keys)}")

        if "raw_row_json" in parsed:
            raw_payload = parsed["raw_row_json"]
            columns = raw_payload.get("columns", [])
            expected_columns = list(MARKET_DEFINITION_COLUMNS[strategic_market_id])
            actual_columns = [column.get("column_id") for column in columns]
            if raw_payload.get("source_sheet") != "시장정의 & Target":
                row_errors.append("raw_row_json.source_sheet mismatch")
            if actual_columns != expected_columns:
                row_errors.append(
                    f"raw_row_json columns expected={expected_columns} actual={actual_columns}"
                )
            for column_payload in columns:
                for cell in column_payload.get("values", []):
                    row_id = cell.get("row_id")
                    if not isinstance(row_id, int) or row_id < 5 or row_id > 64:
                        row_errors.append(f"raw_row_json row_id out of range: {row_id}")
                        break

        for column in FOUR_STRUCTURED_JSON_COLUMNS:
            if column in parsed and not isinstance(parsed[column], (list, dict)):
                row_errors.append(f"{column}: unexpected JSON root")

        if row_errors:
            print(f"  FAIL {strategic_market_id}: {row_errors}")
            fail_count += 1
        else:
            raw_size = json_size(parsed["raw_row_json"])
            print(f"  PASS {strategic_market_id}: JSON valid, raw_payload_values={raw_size}")
            pass_count += 1

    print(f"\n  [Phase 3 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def phase4_schema_pk(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] PK uniqueness / schema 정합성 / ingested_at")
    print("=" * 72)

    failures = []
    ids = [row["strategic_market_id"] for row in parquet_rows]
    if len(ids) != 16 or len(set(ids)) != 16:
        failures.append(f"strategic_market_id uniqueness failed: {ids}")
    if tuple(ids) != EXPECTED_STRATEGIC_MARKET_IDS:
        failures.append(
            f"strategic_market_id order mismatch: expected={EXPECTED_STRATEGIC_MARKET_IDS}, actual={tuple(ids)}"
        )

    for index, row in enumerate(parquet_rows, start=1):
        columns = tuple(row.keys())
        if columns != MASTER_MARKET_DEFINITION_COLUMNS:
            failures.append(f"row {index} schema mismatch: {columns}")
            break
        ingested_at = row.get("ingested_at")
        if not isinstance(ingested_at, str) or not INGESTED_AT_RE.match(ingested_at):
            failures.append(f"row {index} ingested_at format invalid: {ingested_at!r}")

    print(f"  strategic_market_id count: {len(ids)}")
    print(f"  unique strategic_market_id: {len(set(ids))}")
    print(f"  DDL columns only: {len(MASTER_MARKET_DEFINITION_COLUMNS)}")
    print(f"  ingested_at examples: {sorted({row['ingested_at'] for row in parquet_rows})}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)

    print("  result: PASS")
    return 1, 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", default=str(DEFAULT_INPUT_FILE))
    parser.add_argument("--parquet-file", default=str(DEFAULT_OUTPUT_FILE))
    args = parser.parse_args()

    input_file = Path(args.input_file)
    parquet_file = Path(args.parquet_file)

    print("=" * 72)
    print("Verify MI Master market_definition Parquet")
    print("=" * 72)
    print(f"  input file:   {input_file}")
    print(f"  parquet file: {parquet_file}")

    expected_rows = load_expected_rows(input_file)
    parquet_rows = load_parquet_rows(parquet_file)

    phase_results = []
    phase_results.append(("Phase 0", *phase0_full_count(expected_rows, parquet_rows)))
    phase1_stats(parquet_rows)
    phase_results.append(("Phase 2", *phase2_full_row_compare(expected_rows, parquet_rows)))
    phase_results.append(("Phase 3", *phase3_json_validity(parquet_rows)))
    phase_results.append(("Phase 4", *phase4_schema_pk(parquet_rows)))

    total_fail = sum(fail for _, _, fail in phase_results)
    print()
    print("=" * 72)
    print("Verification Summary")
    print("=" * 72)
    for phase, passed, failed in phase_results:
        print(f"  {phase}: pass={passed} fail={failed}")
    print(f"  total failures: {total_fail}")

    if total_fail:
        print("\nFAIL")
        sys.exit(1)
    print("\nPASS — 전수 검증 16/16 통과")


if __name__ == "__main__":
    main()
