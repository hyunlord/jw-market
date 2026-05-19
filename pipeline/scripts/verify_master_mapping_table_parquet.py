"""
verify_master_mapping_table_parquet.py
======================================
Verify Phase 09d MI Master mapping_table Parquet against the raw workbook.

Large-data policy:
- 5,932 rows total, but generation is deterministic and small enough for
  full-row comparison.
- Expected rows are regenerated from the same canonical Phase 09d extraction
  logic used by prototype_10_master_mapping_table_to_parquet.py.
- ingested_at is checked for format only, not value equality.
- SQLite is used only as corroboration against the existing Phase 2 project
  staging artifact.

Usage:
    python3 scripts/verify_master_mapping_table_parquet.py
"""

from __future__ import annotations

import argparse
import re
import sqlite3
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

from prototype_10_master_mapping_table_to_parquet import (  # noqa: E402
    DEFAULT_CATALOG_PATH,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_MAPPING_TYPE_DISTRIBUTION,
    EXPECTED_MARKET_DISTRIBUTION,
    EXPECTED_ROW_COUNT,
    MASTER_MAPPING_TABLE_COLUMNS,
    ZERO_MAPPING_MARKETS,
    _count_by,
    load_mapping_records,
    resolve_input_file,
    validate_records,
)


DEFAULT_SQLITE_PATH = Path("/Users/rexxa/github/jw-market/outputs/phase2_master_staging.sqlite")
INGESTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
MAPPING_ID_RE = re.compile(r"^strategy_\d{3}:[0-9a-f]{16}$")
EXPECTED_MAPPING_TYPES = set(EXPECTED_MAPPING_TYPE_DISTRIBUTION)
ALL_MARKETS = {f"strategy_{index:03d}" for index in range(1, 17)}


def load_parquet_rows(parquet_file: Path) -> tuple[list[dict[str, Any]], Any]:
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    table = pq.read_table(parquet_file)
    return table.to_pylist(), table.schema


def load_expected_rows(
    input_file: Path,
    catalog_path: Path,
) -> tuple[list[dict[str, Any]], Any]:
    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")
    if not catalog_path.exists():
        sys.exit(f"ERROR: catalog file not found: {catalog_path}")
    records, stats = load_mapping_records(
        input_file,
        catalog_path,
        ingested_at="__VERIFY_INGESTED_AT__",
    )
    validate_records(records, stats)
    return records, stats


def normalize_for_compare(record: dict[str, Any]) -> dict[str, Any]:
    normalized = {column: record.get(column) for column in MASTER_MAPPING_TABLE_COLUMNS}
    normalized["ingested_at"] = "__VERIFY_INGESTED_AT__"
    return normalized


def keyed_by_mapping_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        mapping_id = row["mapping_id"]
        if mapping_id in keyed:
            raise ValueError(f"duplicate mapping_id during dict build: {mapping_id}")
        keyed[mapping_id] = row
    return keyed


def phase0_full_count(
    expected_rows: list[dict[str, Any]],
    parquet_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 0] 전수 row count 검증")
    print("=" * 72)

    raw_n = len(expected_rows)
    parquet_n = len(parquet_rows)
    ok = raw_n == parquet_n == EXPECTED_ROW_COUNT
    print(f"  raw recalculated rows: {raw_n}")
    print(f"  parquet rows:          {parquet_n}")
    print(f"  required rows:         {EXPECTED_ROW_COUNT}")
    print(f"  result:                {'PASS' if ok else 'FAIL'}")
    return (1, 0) if ok else (0, 1)


def phase1_stats(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계 / 분포")
    print("=" * 72)

    mapping_ids = [row["mapping_id"] for row in parquet_rows]
    market_distribution = Counter(row["strategic_market_id"] for row in parquet_rows)
    type_distribution = Counter(row["mapping_type"] for row in parquet_rows)
    source_sheet_distribution = Counter(row["source_sheet"] for row in parquet_rows)
    zero_market_counts = {market_id: market_distribution.get(market_id, 0) for market_id in ZERO_MAPPING_MARKETS}
    failures = []

    if len(mapping_ids) != EXPECTED_ROW_COUNT:
        failures.append(f"mapping_id list length mismatch: {len(mapping_ids)}")
    if dict(market_distribution) != EXPECTED_MARKET_DISTRIBUTION:
        failures.append(
            f"strategic_market_id distribution mismatch: {dict(market_distribution)}"
        )
    if dict(type_distribution) != EXPECTED_MAPPING_TYPE_DISTRIBUTION:
        failures.append(f"mapping_type distribution mismatch: {dict(type_distribution)}")
    if any(count != 0 for count in zero_market_counts.values()):
        failures.append(f"zero market has parquet rows: {zero_market_counts}")

    print(f"  mapping_id count:       {len(mapping_ids)}")
    print(f"  unique mapping_id:      {len(set(mapping_ids))}")
    print("\n  strategic_market_id distribution:")
    for key in sorted(ALL_MARKETS):
        print(f"    {key}: {market_distribution.get(key, 0)}")
    print("\n  mapping_type distribution:")
    for key, expected_count in EXPECTED_MAPPING_TYPE_DISTRIBUTION.items():
        print(f"    {key}: {type_distribution.get(key, 0)} (expected {expected_count})")
    print("\n  source_sheet distribution:")
    for key, count in sorted(source_sheet_distribution.items()):
        print(f"    {key}: {count}")
    print(f"\n  zero mapping markets: {zero_market_counts}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def phase2_full_row_compare(
    expected_rows: list[dict[str, Any]],
    parquet_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 2] 5,932 row 전수 raw 비교")
    print("=" * 72)

    expected_by_id = keyed_by_mapping_id(expected_rows)
    parquet_by_id = keyed_by_mapping_id(parquet_rows)
    pass_count = 0
    fail_count = 0
    mismatch_examples = []

    expected_keys = set(expected_by_id)
    parquet_keys = set(parquet_by_id)
    missing_in_parquet = sorted(expected_keys - parquet_keys)
    extra_in_parquet = sorted(parquet_keys - expected_keys)
    if missing_in_parquet or extra_in_parquet:
        print(f"  FAIL key set mismatch: missing={len(missing_in_parquet)}, extra={len(extra_in_parquet)}")
        if missing_in_parquet:
            print(f"    missing examples: {missing_in_parquet[:5]}")
        if extra_in_parquet:
            print(f"    extra examples: {extra_in_parquet[:5]}")
        return 0, len(missing_in_parquet) + len(extra_in_parquet)

    for mapping_id in sorted(expected_keys):
        expected = normalize_for_compare(expected_by_id[mapping_id])
        actual = normalize_for_compare(parquet_by_id[mapping_id])
        if actual == expected:
            pass_count += 1
            continue

        fail_count += 1
        if len(mismatch_examples) < 5:
            mismatched_columns = [
                column
                for column in MASTER_MAPPING_TABLE_COLUMNS
                if actual.get(column) != expected.get(column)
            ]
            mismatch_examples.append(
                {
                    "mapping_id": mapping_id,
                    "columns": mismatched_columns,
                    "actual": {column: actual.get(column) for column in mismatched_columns},
                    "expected": {column: expected.get(column) for column in mismatched_columns},
                }
            )

    print(f"  raw key count:      {len(expected_keys)}")
    print(f"  parquet key count:  {len(parquet_keys)}")
    print(f"  pass rows:          {pass_count}")
    print(f"  fail rows:          {fail_count}")
    if mismatch_examples:
        print("  mismatch examples:")
        for example in mismatch_examples:
            print(f"    {example}")

    return pass_count, fail_count


def phase3_pk_integrity(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 3] PK uniqueness / integrity")
    print("=" * 72)

    failures = []
    mapping_ids = [row["mapping_id"] for row in parquet_rows]
    if len(mapping_ids) != EXPECTED_ROW_COUNT or len(set(mapping_ids)) != EXPECTED_ROW_COUNT:
        failures.append(
            f"mapping_id uniqueness failed: count={len(mapping_ids)}, unique={len(set(mapping_ids))}"
        )

    bad_mapping_id_examples = [mapping_id for mapping_id in mapping_ids if not MAPPING_ID_RE.match(mapping_id)]
    if bad_mapping_id_examples:
        failures.append(f"mapping_id format violations: {bad_mapping_id_examples[:5]}")

    blank_target_examples = [
        row["mapping_id"] for row in parquet_rows if row.get("target_column") is None or str(row.get("target_column")).strip() == ""
    ]
    if blank_target_examples:
        failures.append(f"blank target_column examples: {blank_target_examples[:5]}")

    actual_types = {row["mapping_type"] for row in parquet_rows}
    unexpected_types = actual_types - EXPECTED_MAPPING_TYPES
    if unexpected_types:
        failures.append(f"unexpected mapping_type values: {sorted(unexpected_types)}")

    print(f"  mapping_id count:            {len(mapping_ids)}")
    print(f"  unique mapping_id:           {len(set(mapping_ids))}")
    print(f"  mapping_id format violations:{len(bad_mapping_id_examples)}")
    print(f"  blank target_column rows:    {len(blank_target_examples)}")
    print(f"  mapping_type set:            {sorted(actual_types)}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def phase4_schema(parquet_rows: list[dict[str, Any]], parquet_schema: Any) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] schema 정합성 / dtype / ingested_at")
    print("=" * 72)

    failures = []
    schema_names = tuple(parquet_schema.names)
    if schema_names != MASTER_MAPPING_TABLE_COLUMNS:
        failures.append(f"schema column mismatch: {schema_names}")

    for field in parquet_schema:
        if str(field.type) != "string":
            failures.append(f"column dtype is not string: {field.name}={field.type}")

    forbidden = {"source_files", "period", "raw_row_json", "application_actions_json"}
    present_forbidden = forbidden & set(schema_names)
    if present_forbidden:
        failures.append(f"forbidden helper/JSON columns present: {sorted(present_forbidden)}")

    bad_ingested_at = []
    for index, row in enumerate(parquet_rows, start=1):
        columns = tuple(row.keys())
        if columns != MASTER_MAPPING_TABLE_COLUMNS:
            failures.append(f"row {index} schema mismatch: {columns}")
            break
        ingested_at = row.get("ingested_at")
        if not isinstance(ingested_at, str) or not INGESTED_AT_RE.match(ingested_at):
            bad_ingested_at.append((row.get("mapping_id"), ingested_at))
            if len(bad_ingested_at) >= 5:
                break
    if bad_ingested_at:
        failures.append(f"ingested_at format invalid examples: {bad_ingested_at}")

    print(f"  DDL columns:       {len(MASTER_MAPPING_TABLE_COLUMNS)}")
    print(f"  schema names:      {schema_names}")
    print("  column dtypes:")
    for field in parquet_schema:
        print(f"    {field.name}: {field.type}")
    print("  JSON columns:      none")
    print("  helper columns:    none")
    print(f"  ingested_at values:{sorted({row['ingested_at'] for row in parquet_rows})}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def load_sqlite_rows(sqlite_path: Path) -> list[dict[str, Any]]:
    if not sqlite_path.exists():
        sys.exit(f"ERROR: SQLite corroboration file not found: {sqlite_path}")
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT mapping_id, strategic_market_id, mapping_type
            FROM stg_master_mapping_table
            """
        ).fetchall()
    return [dict(row) for row in rows]


def phase5_sqlite_corroboration(
    parquet_rows: list[dict[str, Any]],
    sqlite_path: Path,
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 5] SQLite corroboration")
    print("=" * 72)

    sqlite_rows = load_sqlite_rows(sqlite_path)
    sqlite_market_distribution = Counter(row["strategic_market_id"] for row in sqlite_rows)
    sqlite_type_distribution = Counter(row["mapping_type"] for row in sqlite_rows)
    sqlite_mapping_ids = {row["mapping_id"] for row in sqlite_rows}
    parquet_mapping_ids = {row["mapping_id"] for row in parquet_rows}
    failures = []

    if len(sqlite_rows) != EXPECTED_ROW_COUNT:
        failures.append(f"SQLite row count mismatch: {len(sqlite_rows)}")
    if dict(sqlite_market_distribution) != EXPECTED_MARKET_DISTRIBUTION:
        failures.append(f"SQLite market distribution mismatch: {dict(sqlite_market_distribution)}")
    if dict(sqlite_type_distribution) != EXPECTED_MAPPING_TYPE_DISTRIBUTION:
        failures.append(f"SQLite mapping_type distribution mismatch: {dict(sqlite_type_distribution)}")
    if sqlite_mapping_ids != parquet_mapping_ids:
        missing_in_sqlite = sorted(parquet_mapping_ids - sqlite_mapping_ids)
        missing_in_parquet = sorted(sqlite_mapping_ids - parquet_mapping_ids)
        failures.append(
            f"SQLite/parquet mapping_id set mismatch: "
            f"missing_in_sqlite={len(missing_in_sqlite)}, missing_in_parquet={len(missing_in_parquet)}"
        )
        if missing_in_sqlite:
            print(f"  parquet-only examples: {missing_in_sqlite[:5]}")
        if missing_in_parquet:
            print(f"  sqlite-only examples: {missing_in_parquet[:5]}")

    print(f"  SQLite path:          {sqlite_path}")
    print(f"  SQLite rows:          {len(sqlite_rows)}")
    print(f"  SQLite unique IDs:    {len(sqlite_mapping_ids)}")
    print(f"  Parquet unique IDs:   {len(parquet_mapping_ids)}")
    print("\n  SQLite market distribution:")
    for key in sorted(ALL_MARKETS):
        print(f"    {key}: {sqlite_market_distribution.get(key, 0)}")
    print("\n  SQLite mapping_type distribution:")
    for key, expected_count in EXPECTED_MAPPING_TYPE_DISTRIBUTION.items():
        print(f"    {key}: {sqlite_type_distribution.get(key, 0)} (expected {expected_count})")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", default=str(DEFAULT_INPUT_FILE))
    parser.add_argument("--catalog-path", default=str(DEFAULT_CATALOG_PATH))
    parser.add_argument("--parquet-file", default=str(DEFAULT_OUTPUT_FILE))
    parser.add_argument("--sqlite-file", default=str(DEFAULT_SQLITE_PATH))
    args = parser.parse_args()

    input_file = resolve_input_file(Path(args.input_file))
    catalog_path = Path(args.catalog_path)
    parquet_file = Path(args.parquet_file)
    sqlite_file = Path(args.sqlite_file)

    print("=" * 72)
    print("Verify MI Master mapping_table Parquet")
    print("=" * 72)
    print(f"  input file:   {input_file}")
    print(f"  catalog file: {catalog_path}")
    print(f"  parquet file: {parquet_file}")
    print(f"  sqlite file:  {sqlite_file}")

    expected_rows, _raw_stats = load_expected_rows(input_file, catalog_path)
    parquet_rows, parquet_schema = load_parquet_rows(parquet_file)

    phase_results = []
    phase_results.append(("Phase 0", *phase0_full_count(expected_rows, parquet_rows)))
    phase_results.append(("Phase 1", *phase1_stats(parquet_rows)))
    phase_results.append(("Phase 2", *phase2_full_row_compare(expected_rows, parquet_rows)))
    phase_results.append(("Phase 3", *phase3_pk_integrity(parquet_rows)))
    phase_results.append(("Phase 4", *phase4_schema(parquet_rows, parquet_schema)))
    phase_results.append(("Phase 5", *phase5_sqlite_corroboration(parquet_rows, sqlite_file)))

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
