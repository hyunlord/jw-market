"""
verify_master_brand_consolidation_parquet.py
============================================
Verify Phase 09c MI Master brand_consolidation Parquet against raw workbook.

Small-data policy:
- 6 rows total, so Phase 2 compares every row, not samples.
- Expected rows are regenerated from the same canonical Phase 09c extraction
  logic used by prototype_09_master_brand_consolidation_to_parquet.py.
- ingested_at is checked for format only, not value equality.

Usage:
    python3 scripts/verify_master_brand_consolidation_parquet.py
"""

from __future__ import annotations

import argparse
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

from prototype_09_master_brand_consolidation_to_parquet import (  # noqa: E402
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_BRAND_GROUP_COUNTS,
    EXPECTED_DRUG_ROWS,
    EXPECTED_MEMBER_DRUG_INDEXES,
    EXPECTED_ROW_COUNT,
    MASTER_BRAND_CONSOLIDATION_COLUMNS,
    SOURCE_REMARK,
    SOURCE_SHEET,
    STRATEGIC_MARKET_ID,
    load_brand_consolidation_records,
)


EXPECTED_BRAND_GROUPS = set(EXPECTED_BRAND_GROUP_COUNTS)
INGESTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def load_parquet_rows(parquet_file: Path) -> list[dict[str, Any]]:
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    table = pq.read_table(parquet_file)
    return table.to_pylist()


def load_expected_rows(input_file: Path) -> tuple[list[dict[str, Any]], Any]:
    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")
    return load_brand_consolidation_records(input_file, ingested_at="__VERIFY_INGESTED_AT__")


def normalize_for_compare(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["member_drug_index"] = int(normalized["member_drug_index"])
    normalized["ingested_at"] = "__VERIFY_INGESTED_AT__"
    return normalized


def pk_tuple(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        row["strategic_market_id"],
        row["brand_group"],
        int(row["member_drug_index"]),
    )


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
    print(f"  raw rows scanned:          {raw_stats.raw_rows_scanned}")
    print(f"  raw empty rows:            {raw_stats.empty_rows}")
    print(f"  raw excluded rows:         {raw_stats.excluded_rows}")
    print(f"  raw staging drug rows:     {raw_stats.staging_drug_rows}")
    print(f"  raw brand rows:            {raw_stats.brand_consolidation_rows}")
    print(f"  parquet rows:              {parquet_n}")
    print(f"  required brand rows:       {EXPECTED_ROW_COUNT}")
    print(f"  result:                    {'PASS' if ok else 'FAIL'}")
    return (1, 0) if ok else (0, 1)


def phase1_stats(parquet_rows: list[dict[str, Any]]) -> dict[str, Any]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)

    pk_values = [pk_tuple(row) for row in parquet_rows]
    brand_group_counts = Counter(row["brand_group"] for row in parquet_rows)
    source_sheet_counts = Counter(row["source_sheet"] for row in parquet_rows)
    source_remark_counts = Counter(row["source_remark"] for row in parquet_rows)

    print("  compound PK list:")
    for strategic_market_id, brand_group, member_drug_index in pk_values:
        print(f"    {strategic_market_id} | {brand_group} | {member_drug_index}")

    print("\n  brand_group distribution:")
    for key, count in sorted(brand_group_counts.items()):
        print(f"    {key}: {count}")

    print("\n  source_sheet distribution:")
    for key, count in sorted(source_sheet_counts.items()):
        print(f"    {key}: {count}")

    print("\n  source_remark distribution:")
    for key, count in sorted(source_remark_counts.items()):
        print(f"    {key}: {count}")

    return {
        "pk_values": pk_values,
        "brand_group_counts": dict(brand_group_counts),
        "source_sheet_counts": dict(source_sheet_counts),
        "source_remark_counts": dict(source_remark_counts),
    }


def phase2_full_row_compare(
    expected_rows: list[dict[str, Any]],
    parquet_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 2] 6 row 전수 raw 비교")
    print("=" * 72)

    expected_by_pk = {pk_tuple(row): normalize_for_compare(row) for row in expected_rows}
    pass_count = 0
    fail_count = 0

    for index, actual in enumerate(parquet_rows, start=1):
        actual_pk = pk_tuple(actual)
        expected = expected_by_pk.get(actual_pk)
        title = f"{actual['strategic_market_id']} | {actual['brand_group']} | {actual['member_drug_index']}"
        if expected is None:
            print(f"  [{index:02d}] FAIL missing expected row: {title}")
            fail_count += 1
            continue

        actual_normalized = normalize_for_compare(actual)
        mismatches = []
        for column in MASTER_BRAND_CONSOLIDATION_COLUMNS:
            if actual_normalized.get(column) != expected.get(column):
                mismatches.append(column)

        if actual["strategic_market_id"] != STRATEGIC_MARKET_ID:
            mismatches.append("strategic_market_id")
        if actual["brand_group"] not in EXPECTED_BRAND_GROUPS:
            mismatches.append("brand_group")
        if int(actual["member_drug_index"]) not in EXPECTED_MEMBER_DRUG_INDEXES:
            mismatches.append("member_drug_index")
        if actual["source_remark"] != SOURCE_REMARK:
            mismatches.append("source_remark")
        if actual["source_sheet"] != SOURCE_SHEET:
            mismatches.append("source_sheet")

        if mismatches:
            print(f"  [{index:02d}] FAIL {title}: {mismatches}")
            fail_count += 1
        else:
            print(f"  [{index:02d}] PASS {title} | {actual['member_drug_name']}")
            pass_count += 1

    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def phase3_compound_pk(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 3] 복합 PK uniqueness")
    print("=" * 72)

    failures = []
    pk_values = [pk_tuple(row) for row in parquet_rows]
    member_indexes = {int(row["member_drug_index"]) for row in parquet_rows}
    brand_groups = {row["brand_group"] for row in parquet_rows}

    if len(pk_values) != EXPECTED_ROW_COUNT or len(set(pk_values)) != EXPECTED_ROW_COUNT:
        failures.append(f"compound PK uniqueness failed: {pk_values}")
    if member_indexes != EXPECTED_MEMBER_DRUG_INDEXES:
        failures.append(
            f"member_drug_index mismatch: "
            f"expected={sorted(EXPECTED_MEMBER_DRUG_INDEXES)}, actual={sorted(member_indexes)}"
        )
    if brand_groups != EXPECTED_BRAND_GROUPS:
        failures.append(
            f"brand_group set mismatch: "
            f"expected={sorted(EXPECTED_BRAND_GROUPS)}, actual={sorted(brand_groups)}"
        )

    print(f"  compound PK count:       {len(pk_values)}")
    print(f"  unique compound PK:      {len(set(pk_values))}")
    print(f"  member_drug_index set:   {sorted(member_indexes)}")
    print(f"  brand_group set:         {sorted(brand_groups)}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)

    print("  result: PASS")
    return 1, 0


def phase4_schema(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] schema 정합성 / ingested_at")
    print("=" * 72)

    failures = []
    for index, row in enumerate(parquet_rows, start=1):
        columns = tuple(row.keys())
        if columns != MASTER_BRAND_CONSOLIDATION_COLUMNS:
            failures.append(f"row {index} schema mismatch: {columns}")
            break
        for forbidden in ("source_files", "period"):
            if forbidden in row:
                failures.append(f"row {index} helper column present: {forbidden}")
        ingested_at = row.get("ingested_at")
        if not isinstance(ingested_at, str) or not INGESTED_AT_RE.match(ingested_at):
            failures.append(f"row {index} ingested_at format invalid: {ingested_at!r}")
        try:
            int(row["member_drug_index"])
        except Exception as exc:  # noqa: BLE001
            failures.append(f"row {index} member_drug_index not int-convertible: {exc}")

    print(f"  DDL columns only:    {len(MASTER_BRAND_CONSOLIDATION_COLUMNS)}")
    print("  JSON columns:        none")
    print("  helper columns:      none")
    print(f"  ingested_at values:  {sorted({row['ingested_at'] for row in parquet_rows})}")

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
    print("Verify MI Master brand_consolidation Parquet")
    print("=" * 72)
    print(f"  input file:   {input_file}")
    print(f"  parquet file: {parquet_file}")

    expected_rows, raw_stats = load_expected_rows(input_file)
    parquet_rows = load_parquet_rows(parquet_file)

    phase_results = []
    phase_results.append(("Phase 0", *phase0_full_count(expected_rows, parquet_rows, raw_stats)))
    phase1_stats(parquet_rows)
    phase_results.append(("Phase 2", *phase2_full_row_compare(expected_rows, parquet_rows)))
    phase_results.append(("Phase 3", *phase3_compound_pk(parquet_rows)))
    phase_results.append(("Phase 4", *phase4_schema(parquet_rows)))

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
