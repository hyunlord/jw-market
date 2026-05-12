"""
verify_master_qa_parquet.py
===========================
Verify Phase 09b MI Master Q&A Parquet against the raw workbook.

Small-data policy:
- 13 rows total, so Phase 2 compares every row, not samples.
- Expected rows are regenerated from the same canonical Phase 09b extraction
  logic used by prototype_08_master_qa_to_parquet.py.
- ingested_at is checked for format only, not value equality.

Usage:
    python3 scripts/verify_master_qa_parquet.py
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

from prototype_08_master_qa_to_parquet import (  # noqa: E402
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_QA_IDS,
    EXPECTED_ROW_COUNT,
    MASTER_QA_COLUMNS,
    load_qa_records,
    market_id_for_name,
)


EXPECTED_ACTION_KEYS = {
    "question_type",
    "market_name",
    "raw_answer",
    "raw_marketing_note",
    "auto_apply_in_phase_2",
    "raw_row",
}

INGESTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def load_parquet_rows(parquet_file: Path) -> list[dict[str, Any]]:
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    table = pq.read_table(parquet_file)
    return table.to_pylist()


def load_expected_rows(input_file: Path) -> tuple[list[dict[str, Any]], Any]:
    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")
    return load_qa_records(input_file, ingested_at="__VERIFY_INGESTED_AT__")


def parse_actions(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["application_actions_json"])


def normalize_for_compare(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["ingested_at"] = "__VERIFY_INGESTED_AT__"
    normalized["application_actions_json"] = json.loads(record["application_actions_json"])
    return normalized


def phase0_full_count(
    expected_rows: list[dict[str, Any]],
    parquet_rows: list[dict[str, Any]],
    raw_stats: Any,
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 0] 전수 row count 검증")
    print("=" * 72)

    raw_n = len(expected_rows)
    parquet_n = len(parquet_rows)
    ok = raw_n == parquet_n == EXPECTED_ROW_COUNT
    print(f"  raw rows scanned: {raw_stats.raw_rows_scanned}")
    print(f"  raw empty rows:   {raw_stats.empty_rows}")
    print(f"  raw staging rows: {raw_stats.staging_rows}")
    print(f"  parquet rows:     {parquet_n}")
    print(f"  required rows:    {EXPECTED_ROW_COUNT}")
    print(f"  result:           {'PASS' if ok else 'FAIL'}")
    return (1, 0) if ok else (0, 1)


def phase1_stats(parquet_rows: list[dict[str, Any]]) -> dict[str, Any]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)

    qa_ids = [row["qa_id"] for row in parquet_rows]
    strategic_market_counts = Counter(
        row["strategic_market_id"] if row["strategic_market_id"] is not None else "NULL"
        for row in parquet_rows
    )
    source_sheet_counts = Counter(row["source_sheet"] for row in parquet_rows)
    source_file_counts = Counter(row["source_file_version"] for row in parquet_rows)

    print("  qa_id list:")
    for qa_id in qa_ids:
        print(f"    {qa_id}")

    print("\n  strategic_market_id distribution:")
    for key, count in sorted(strategic_market_counts.items()):
        print(f"    {key}: {count}")

    print("\n  source_sheet distribution:")
    for key, count in sorted(source_sheet_counts.items()):
        print(f"    {key}: {count}")

    print("\n  source_file_version distribution:")
    for key, count in sorted(source_file_counts.items()):
        print(f"    {key}: {count}")

    return {
        "qa_ids": qa_ids,
        "strategic_market_counts": dict(strategic_market_counts),
        "source_sheet_counts": dict(source_sheet_counts),
        "source_file_counts": dict(source_file_counts),
    }


def phase2_full_row_compare(
    expected_rows: list[dict[str, Any]],
    parquet_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 2] 13 row 전수 raw 비교")
    print("=" * 72)

    expected_by_id = {row["qa_id"]: row for row in expected_rows}
    pass_count = 0
    fail_count = 0

    for index, actual in enumerate(parquet_rows, start=1):
        qa_id = actual["qa_id"]
        expected = expected_by_id.get(qa_id)
        if expected is None:
            print(f"  [{index:02d}] FAIL missing expected row: {qa_id}")
            fail_count += 1
            continue

        actual_actions = parse_actions(actual)
        expected_actions = parse_actions(expected)
        market_name = actual_actions.get("market_name")
        question_text = (actual.get("question_text") or "").replace("\n", " ")[:50]
        mismatches = []

        if qa_id != EXPECTED_QA_IDS[index - 1]:
            mismatches.append("qa_id sequence")
        if actual.get("strategic_market_id") != market_id_for_name(market_name):
            mismatches.append("strategic_market_id(market_id_for_name)")

        for column in (
            "qa_id",
            "strategic_market_id",
            "question_text",
            "answer_text",
            "source_remark",
            "source_sheet",
            "source_file_version",
        ):
            if actual.get(column) != expected.get(column):
                mismatches.append(column)

        if actual_actions != expected_actions:
            mismatches.append("application_actions_json")

        if set(actual_actions) != EXPECTED_ACTION_KEYS:
            mismatches.append("application_actions_json.keys")

        if mismatches:
            print(f"  [{index:02d}] FAIL {qa_id} | {market_name} | {mismatches}")
            fail_count += 1
        else:
            print(f"  [{index:02d}] PASS {qa_id} | {market_name} | {question_text}")
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

    for index, row in enumerate(parquet_rows, start=1):
        qa_id = row["qa_id"]
        row_errors = []
        try:
            actions = parse_actions(row)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {qa_id}: invalid JSON ({exc})")
            fail_count += 1
            continue

        if set(actions) != EXPECTED_ACTION_KEYS:
            row_errors.append(f"keys={sorted(actions)}")
        if actions.get("auto_apply_in_phase_2") is not False:
            row_errors.append("auto_apply_in_phase_2 is not False")

        raw_row = actions.get("raw_row")
        expected_source_row_id = index + 2
        if not isinstance(raw_row, dict):
            row_errors.append("raw_row is not object")
        else:
            if raw_row.get("source_row_id") != expected_source_row_id:
                row_errors.append(
                    f"raw_row.source_row_id expected={expected_source_row_id} "
                    f"actual={raw_row.get('source_row_id')}"
                )
            if not isinstance(raw_row.get("cells"), list):
                row_errors.append("raw_row.cells is not list")
            if not isinstance(raw_row.get("values_by_header"), dict):
                row_errors.append("raw_row.values_by_header is not object")

        if row_errors:
            print(f"  FAIL {qa_id}: {row_errors}")
            fail_count += 1
        else:
            print(
                f"  PASS {qa_id}: JSON valid, "
                f"source_row_id={raw_row.get('source_row_id')}"
            )
            pass_count += 1

    print(f"\n  [Phase 3 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def phase4_schema_pk(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] PK uniqueness / schema 정합성 / ingested_at")
    print("=" * 72)

    failures = []
    qa_ids = [row["qa_id"] for row in parquet_rows]
    if len(qa_ids) != EXPECTED_ROW_COUNT or len(set(qa_ids)) != EXPECTED_ROW_COUNT:
        failures.append(f"qa_id uniqueness failed: {qa_ids}")
    if tuple(qa_ids) != EXPECTED_QA_IDS:
        failures.append(f"qa_id order mismatch: expected={EXPECTED_QA_IDS}, actual={tuple(qa_ids)}")

    for index, row in enumerate(parquet_rows, start=1):
        columns = tuple(row.keys())
        if columns != MASTER_QA_COLUMNS:
            failures.append(f"row {index} schema mismatch: {columns}")
            break
        ingested_at = row.get("ingested_at")
        if not isinstance(ingested_at, str) or not INGESTED_AT_RE.match(ingested_at):
            failures.append(f"row {index} ingested_at format invalid: {ingested_at!r}")

    print(f"  qa_id count:        {len(qa_ids)}")
    print(f"  unique qa_id:       {len(set(qa_ids))}")
    print(f"  DDL columns only:   {len(MASTER_QA_COLUMNS)}")
    print(f"  ingested_at values: {sorted({row['ingested_at'] for row in parquet_rows})}")

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
    print("Verify MI Master Q&A Parquet")
    print("=" * 72)
    print(f"  input file:   {input_file}")
    print(f"  parquet file: {parquet_file}")

    expected_rows, raw_stats = load_expected_rows(input_file)
    parquet_rows = load_parquet_rows(parquet_file)

    phase_results = []
    phase_results.append(("Phase 0", *phase0_full_count(expected_rows, parquet_rows, raw_stats)))
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
    print(f"\nPASS — 전수 검증 {EXPECTED_ROW_COUNT}/{EXPECTED_ROW_COUNT} 통과")


if __name__ == "__main__":
    main()
