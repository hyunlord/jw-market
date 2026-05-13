"""
verify_dim_jw_products_parquet.py
=================================
Verify Phase 10 dim_jw_products Parquet.

Small-data policy:
- 26 rows total, so Phase 2 compares every row, not samples.
- Expected rows are regenerated from prototype_12_dim_jw_products_to_parquet.py.
- ingested_at is checked for format only, not value equality.

Usage:
    python3 scripts/verify_dim_jw_products_parquet.py
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

from prototype_12_dim_jw_products_to_parquet import (  # noqa: E402
    DEFAULT_MARKET_DEFINITION_FILE,
    DEFAULT_OUTPUT_FILE,
    DEFAULT_QA_FILE,
    DIM_JW_PRODUCTS_COLUMNS,
    EXPECTED_FINAL_ROWS,
    EXPECTED_MARKET_DISTRIBUTION,
    EXPECTED_ROW_COUNT,
    EXPECTED_SOURCE_FILE_VERSION,
    jw_product_id,
    load_dim_jw_product_records,
)


INGESTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
JW_PRODUCT_ID_RE = re.compile(r"^strategy_\d{3}:[0-9a-f]{16}$")
FORBIDDEN_COLUMNS = {"source_files", "period", "master_drug_indexes_json"}


def load_parquet_rows(parquet_file: Path) -> list[dict[str, Any]]:
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    return pq.read_table(parquet_file).to_pylist()


def load_parquet_table(parquet_file: Path):
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    return pq.read_table(parquet_file)


def normalize_for_compare(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["ingested_at"] = "__VERIFY_INGESTED_AT__"
    return normalized


def natural_key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["strategic_market_id"], row["jw_product_name"])


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
    print(f"  regenerated raw rows: {raw_n}")
    print(f"  parquet rows:         {parquet_n}")
    print(f"  required rows:        {EXPECTED_ROW_COUNT}")
    print(f"  result:               {'PASS' if ok else 'FAIL'}")
    return (1, 0) if ok else (0, 1)


def phase1_stats(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)

    market_distribution = Counter(row["strategic_market_id"] for row in parquet_rows)
    source_note_distribution = Counter(row["source_note"] for row in parquet_rows)
    jw_product_ids = [row["jw_product_id"] for row in parquet_rows]

    print("  market distribution:")
    for market_id, count in sorted(market_distribution.items()):
        print(f"    {market_id}: {count}")

    print("\n  source_note distribution:")
    for note, count in source_note_distribution.items():
        print(f"    {count}: {note}")

    print("\n  jw_product_id format:")
    invalid_ids = [value for value in jw_product_ids if not JW_PRODUCT_ID_RE.match(value or "")]
    print(f"    total ids:   {len(jw_product_ids)}")
    print(f"    invalid ids: {len(invalid_ids)}")

    failures = []
    if dict(market_distribution) != EXPECTED_MARKET_DISTRIBUTION:
        failures.append(
            f"market distribution mismatch: expected={EXPECTED_MARKET_DISTRIBUTION}, "
            f"actual={dict(market_distribution)}"
        )
    if invalid_ids:
        failures.append(f"invalid jw_product_id format: {invalid_ids}")

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
    print("[Phase 2] 26 row 전수 7-column 비교")
    print("=" * 72)

    expected_by_key = {natural_key(row): normalize_for_compare(row) for row in expected_rows}
    pass_count = 0
    fail_count = 0

    for index, actual in enumerate(parquet_rows, start=1):
        key = natural_key(actual)
        expected = expected_by_key.get(key)
        if expected is None:
            print(f"  [{index:02d}] FAIL missing expected row: {key}")
            fail_count += 1
            continue

        actual_normalized = normalize_for_compare(actual)
        mismatches = []
        for column in DIM_JW_PRODUCTS_COLUMNS:
            if column == "ingested_at":
                continue
            if actual_normalized.get(column) != expected.get(column):
                mismatches.append(column)

        recalculated_id = jw_product_id(actual["strategic_market_id"], actual["jw_product_name"])
        if actual["jw_product_id"] != recalculated_id:
            mismatches.append("jw_product_id.sha256")
        if actual.get("source_file_version") != EXPECTED_SOURCE_FILE_VERSION:
            mismatches.append("source_file_version")

        if mismatches:
            print(f"  [{index:02d}] FAIL {key}: {mismatches}")
            fail_count += 1
        else:
            print(f"  [{index:02d}] PASS {key[0]} | {key[1]} | {actual['source_note']}")
            pass_count += 1

    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


def phase3_uniqueness(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 3] key uniqueness")
    print("=" * 72)

    ids = [row["jw_product_id"] for row in parquet_rows]
    natural_keys = [natural_key(row) for row in parquet_rows]
    failures = []

    if len(set(ids)) != EXPECTED_ROW_COUNT:
        failures.append(f"jw_product_id unique failed: {len(set(ids))}")
    if len(set(natural_keys)) != EXPECTED_ROW_COUNT:
        failures.append(f"natural key unique failed: {len(set(natural_keys))}")

    print(f"  jw_product_id count:        {len(ids)}")
    print(f"  unique jw_product_id:       {len(set(ids))}")
    print(f"  natural key count:          {len(natural_keys)}")
    print(f"  unique natural key:         {len(set(natural_keys))}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)

    print("  result: PASS")
    return 1, 0


def phase4_source_corroboration(
    market_definition_file: Path,
    qa_file: Path,
    parquet_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] source parquet corroboration")
    print("=" * 72)

    market_rows = load_parquet_rows(market_definition_file)
    qa_rows = load_parquet_rows(qa_file)
    failures = []

    if len(market_rows) != 16:
        failures.append(f"market_definition row count expected=16 actual={len(market_rows)}")

    sheet_tokens = []
    for record in sorted(market_rows, key=lambda row: row["strategic_market_id"]):
        market_name = record.get("market_name")
        if not isinstance(market_name, str) or not market_name.strip():
            failures.append(f"empty market_name for {record.get('strategic_market_id')}")
            continue
        for token in market_name.split():
            sheet_tokens.append((record["strategic_market_id"], token))

    qa_0011 = [row for row in qa_rows if row.get("qa_id") == "qa_0011"]
    if len(qa_0011) != 1:
        failures.append(f"qa_0011 expected once, found={len(qa_0011)}")
    else:
        qa_payload = json.dumps(qa_0011[0], ensure_ascii=False, sort_keys=True)
        if qa_0011[0].get("strategic_market_id") != "strategy_015":
            failures.append("qa_0011 strategic_market_id is not strategy_015")
        if "하모닐란" not in qa_payload:
            failures.append("qa_0011 does not contain 하모닐란")

    parquet_keys = {(row["strategic_market_id"], row["jw_product_name"]) for row in parquet_rows}
    sheet_token_keys = set(sheet_tokens)
    harmonilan_key = ("strategy_015", "하모닐란")

    if len(sheet_tokens) != 25:
        failures.append(f"sheet split token count expected=25 actual={len(sheet_tokens)}")
    if len(sheet_token_keys) != 25:
        failures.append(f"sheet split token unique expected=25 actual={len(sheet_token_keys)}")
    if not sheet_token_keys.issubset(parquet_keys):
        failures.append(f"sheet split tokens missing from parquet: {sorted(sheet_token_keys - parquet_keys)}")
    if harmonilan_key not in parquet_keys:
        failures.append("하모닐란 override row missing from parquet")
    if len(sheet_token_keys | {harmonilan_key}) != EXPECTED_ROW_COUNT:
        failures.append("25 sheet split + 1 하모닐란 override != 26")

    print(f"  market_definition rows: {len(market_rows)}")
    print(f"  sheet split tokens:     {len(sheet_tokens)}")
    print(f"  unique sheet tokens:    {len(sheet_token_keys)}")
    print(f"  qa_0011 rows:           {len(qa_0011)}")
    print(f"  하모닐란 in qa_0011:     {'yes' if qa_0011 and '하모닐란' in json.dumps(qa_0011[0], ensure_ascii=False) else 'no'}")
    print(f"  final row formula:      25 + 1 = {len(sheet_token_keys | {harmonilan_key})}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)

    print("  result: PASS")
    return 1, 0


def phase5_schema(parquet_file: Path, parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 5] schema 정합성 / dtype / ingested_at")
    print("=" * 72)

    table = load_parquet_table(parquet_file)
    failures = []

    if tuple(table.column_names) != DIM_JW_PRODUCTS_COLUMNS:
        failures.append(
            f"column mismatch: expected={DIM_JW_PRODUCTS_COLUMNS}, actual={tuple(table.column_names)}"
        )

    for forbidden in FORBIDDEN_COLUMNS:
        if forbidden in table.column_names:
            failures.append(f"forbidden helper/reference column present: {forbidden}")

    for field in table.schema:
        if not str(field.type).startswith("string"):
            failures.append(f"non-string dtype: {field.name}={field.type}")

    ingested_values = {row.get("ingested_at") for row in parquet_rows}
    for value in ingested_values:
        if not isinstance(value, str) or not INGESTED_AT_RE.match(value):
            failures.append(f"invalid ingested_at: {value!r}")

    print(f"  columns:          {table.column_names}")
    print(f"  column count:     {len(table.column_names)}")
    print(f"  forbidden cols:   {sorted(FORBIDDEN_COLUMNS & set(table.column_names))}")
    print(f"  ingested_at vals: {sorted(ingested_values)}")
    print("  dtypes:")
    for field in table.schema:
        print(f"    {field.name}: {field.type}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)

    print("  result: PASS")
    return 1, 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--qa", type=Path, default=DEFAULT_QA_FILE)
    parser.add_argument("--parquet", type=Path, default=DEFAULT_OUTPUT_FILE)
    args = parser.parse_args()

    expected_rows = load_dim_jw_product_records(
        args.market_definition,
        args.qa,
        ingested_at="__VERIFY_INGESTED_AT__",
    )
    parquet_rows = load_parquet_rows(args.parquet)

    results = []
    results.append(phase0_full_count(expected_rows, parquet_rows))
    results.append(phase1_stats(parquet_rows))
    results.append(phase2_full_row_compare(expected_rows, parquet_rows))
    results.append(phase3_uniqueness(parquet_rows))
    results.append(phase4_source_corroboration(args.market_definition, args.qa, parquet_rows))
    results.append(phase5_schema(args.parquet, parquet_rows))

    pass_total = sum(item[0] for item in results)
    fail_total = sum(item[1] for item in results)

    print()
    print("=" * 72)
    print("[SUMMARY]")
    print("=" * 72)
    print(f"  verification pass units: {pass_total}")
    print(f"  verification fail units: {fail_total}")
    print(f"  full row compare:        {EXPECTED_ROW_COUNT}/{EXPECTED_ROW_COUNT}")

    if fail_total:
        sys.exit(1)

    print("  result: PASS")
    print()
    print("Phase 10 Step E 완료. 검증 결과 검토 필요.")


if __name__ == "__main__":
    main()
