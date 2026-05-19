"""
verify_master_drug_parquet.py
=============================
Verify Phase 09e MI Master drug Parquet against the raw workbook and
the existing project SQLite staging artifact.

Large-data policy:
- 3,912 rows are regenerated deterministically from the same canonical
  Phase 09e extraction logic used by prototype_11_master_drug_to_parquet.py.
- Raw recalculation vs Parquet is a full 31-column comparison, excluding
  ingested_at value equality because it changes on regeneration.
- SQLite corroboration performs full-row comparison with JSON normalization,
  excluding ingested_at value equality.

Usage:
    python3 scripts/verify_master_drug_parquet.py
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from prototype_11_master_drug_to_parquet import (  # noqa: E402
    DEFAULT_CATALOG_PATH,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_MARKET_STATS,
    EXPECTED_ROW_COUNT,
    EXPECTED_SOURCE_TYPE_DISTRIBUTION,
    JSON_COLUMNS,
    MARKET_SHEETS,
    MASTER_DRUG_COLUMNS,
    _expected_extra_keys,
    dumps_json,
    load_column_metadata_catalog,
    load_drug_records,
    resolve_input_file,
    validate_records,
)


DEFAULT_SQLITE_PATH = Path("/Users/rexxa/github/jw-market/outputs/phase2_master_staging.sqlite")
INGESTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
PK_COLUMNS = ("strategic_market_id", "drug_index")
COMPARE_COLUMNS = tuple(column for column in MASTER_DRUG_COLUMNS if column != "ingested_at")
NON_JSON_COMPARE_COLUMNS = tuple(column for column in COMPARE_COLUMNS if column not in JSON_COLUMNS)


def load_parquet_rows(parquet_file: Path) -> tuple[list[dict[str, Any]], Any]:
    if not parquet_file.exists():
        sys.exit(f"ERROR: parquet file not found: {parquet_file}")
    table = pq.read_table(parquet_file)
    return table.to_pylist(), table.schema


def load_expected_rows(input_file: Path, catalog_path: Path) -> tuple[list[dict[str, Any]], Any]:
    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")
    if not catalog_path.exists():
        sys.exit(f"ERROR: catalog file not found: {catalog_path}")
    records, stats = load_drug_records(
        input_file,
        catalog_path,
        ingested_at="__VERIFY_INGESTED_AT__",
    )
    validate_records(records, stats, catalog_path)
    return records, stats


def _as_compare_scalar(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _pk(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row["strategic_market_id"]), str(row["drug_index"]))


def keyed_by_pk(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        pk = _pk(row)
        if pk in keyed:
            raise ValueError(f"duplicate PK during dict build: {pk}")
        keyed[pk] = row
    return keyed


def json_compare(left: Any, right: Any) -> tuple[bool, str]:
    left_text = "" if left is None else str(left)
    right_text = "" if right is None else str(right)
    if left_text == right_text:
        return True, "string"
    try:
        left_obj = json.loads(left_text)
        right_obj = json.loads(right_text)
    except Exception:
        return False, "invalid"
    if left_obj == right_obj:
        return True, "structural"
    return False, "mismatch"


def phase0_full_count(expected_rows: list[dict[str, Any]], parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
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


def phase1_stats(parquet_rows: list[dict[str, Any]], catalog_path: Path) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 1] 통계 / 분포")
    print("=" * 72)

    failures: list[str] = []
    market_distribution = Counter(row["strategic_market_id"] for row in parquet_rows)
    source_distribution = Counter(row["source_type"] for row in parquet_rows)
    metadata_catalog = load_column_metadata_catalog(catalog_path)
    records_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in parquet_rows:
        records_by_market[row["strategic_market_id"]].append(row)

    expected_market_distribution = {
        market_id: expected["staging_rows"] for market_id, expected in EXPECTED_MARKET_STATS.items()
    }
    if dict(sorted(market_distribution.items())) != expected_market_distribution:
        failures.append(f"market distribution mismatch: {dict(sorted(market_distribution.items()))}")
    if dict(sorted(source_distribution.items())) != EXPECTED_SOURCE_TYPE_DISTRIBUTION:
        failures.append(f"source_type distribution mismatch: {dict(sorted(source_distribution.items()))}")

    print("  strategic_market_id distribution:")
    for config in MARKET_SHEETS:
        market_id = config.strategic_market_id
        print(f"    {market_id}: {market_distribution.get(market_id, 0)}")
    print("\n  source_type distribution:")
    for source_type, count in sorted(source_distribution.items()):
        print(f"    {source_type}: {count}")

    print("\n  market JSON/key shape:")
    for config in MARKET_SHEETS:
        market_id = config.strategic_market_id
        metadata = metadata_catalog[market_id]
        expected_extra_keys = _expected_extra_keys(metadata)
        rows = records_by_market[market_id]
        drug_indexes = [int(row["drug_index"]) for row in rows]
        extra_key_sets = {
            tuple(sorted(json.loads(row["drug_extra_json"]).keys()))
            for row in rows
        }
        metadata_key_counts = {
            len(json.loads(row["column_metadata_json"]))
            for row in rows
        }
        if drug_indexes != list(range(1, len(rows) + 1)):
            failures.append(f"{market_id} drug_index not sequential")
        if extra_key_sets != {tuple(expected_extra_keys)}:
            failures.append(f"{market_id} extra key set mismatch: {extra_key_sets}")
        if metadata_key_counts != {len(metadata)}:
            failures.append(f"{market_id} metadata key count mismatch: {metadata_key_counts}")
        print(
            f"    {market_id}: rows={len(rows)}, index=1-{len(rows)}, "
            f"extras={expected_extra_keys or '없음'}, metadata_keys={len(metadata)}"
        )

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def compare_row_values(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> tuple[list[str], dict[str, int]]:
    mismatched_columns: list[str] = []
    json_modes = {"string": 0, "structural": 0, "invalid": 0, "mismatch": 0}

    for column in NON_JSON_COMPARE_COLUMNS:
        if _as_compare_scalar(expected.get(column)) != _as_compare_scalar(actual.get(column)):
            mismatched_columns.append(column)

    for column in JSON_COLUMNS:
        ok, mode = json_compare(expected.get(column), actual.get(column))
        json_modes[mode] = json_modes.get(mode, 0) + 1
        if not ok:
            mismatched_columns.append(column)

    return mismatched_columns, json_modes


def phase2_full_row_compare(expected_rows: list[dict[str, Any]], parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 2] 3,912 row 전수 raw 재계산 vs parquet 비교")
    print("=" * 72)

    expected_by_pk = keyed_by_pk(expected_rows)
    parquet_by_pk = keyed_by_pk(parquet_rows)
    expected_keys = set(expected_by_pk)
    parquet_keys = set(parquet_by_pk)
    pass_count = 0
    fail_count = 0
    json_mode_totals = Counter()
    mismatch_examples = []

    missing_in_parquet = sorted(expected_keys - parquet_keys)
    extra_in_parquet = sorted(parquet_keys - expected_keys)
    if missing_in_parquet or extra_in_parquet:
        print(f"  FAIL key set mismatch: missing={len(missing_in_parquet)}, extra={len(extra_in_parquet)}")
        if missing_in_parquet:
            print(f"    missing examples: {missing_in_parquet[:5]}")
        if extra_in_parquet:
            print(f"    extra examples: {extra_in_parquet[:5]}")
        return 0, len(missing_in_parquet) + len(extra_in_parquet)

    for pk in sorted(expected_keys):
        mismatches, modes = compare_row_values(expected_by_pk[pk], parquet_by_pk[pk])
        json_mode_totals.update(modes)
        if not mismatches:
            pass_count += 1
            continue
        fail_count += 1
        if len(mismatch_examples) < 5:
            mismatch_examples.append(
                {
                    "pk": pk,
                    "columns": mismatches,
                    "actual": {column: parquet_by_pk[pk].get(column) for column in mismatches},
                    "expected": {column: expected_by_pk[pk].get(column) for column in mismatches},
                }
            )

    print(f"  raw key count:      {len(expected_keys)}")
    print(f"  parquet key count:  {len(parquet_keys)}")
    print(f"  pass rows:          {pass_count}")
    print(f"  fail rows:          {fail_count}")
    print(f"  JSON compare modes: {dict(json_mode_totals)}")
    if mismatch_examples:
        print("  mismatch examples:")
        for example in mismatch_examples:
            print(f"    {example}")
    return pass_count, fail_count


def phase3_pk_integrity(parquet_rows: list[dict[str, Any]]) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 3] 복합 PK / drug_index integrity")
    print("=" * 72)

    failures: list[str] = []
    pk_values = [_pk(row) for row in parquet_rows]
    records_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in parquet_rows:
        records_by_market[row["strategic_market_id"]].append(row)

    if len(pk_values) != EXPECTED_ROW_COUNT or len(set(pk_values)) != EXPECTED_ROW_COUNT:
        failures.append(f"PK uniqueness failed: count={len(pk_values)}, unique={len(set(pk_values))}")

    bad_drug_index = []
    bad_source_row_id = []
    for row in parquet_rows:
        try:
            int(row["drug_index"])
        except Exception:
            bad_drug_index.append(_pk(row))
        try:
            int(row["source_row_id"])
        except Exception:
            bad_source_row_id.append(_pk(row))

    if bad_drug_index:
        failures.append(f"drug_index int conversion failures: {bad_drug_index[:5]}")
    if bad_source_row_id:
        failures.append(f"source_row_id int conversion failures: {bad_source_row_id[:5]}")

    print(f"  PK count:        {len(pk_values)}")
    print(f"  unique PK count: {len(set(pk_values))}")
    print("  market ranges:")
    for config in MARKET_SHEETS:
        rows = records_by_market[config.strategic_market_id]
        indexes = [int(row["drug_index"]) for row in rows]
        expected_count = EXPECTED_MARKET_STATS[config.strategic_market_id]["staging_rows"]
        is_sequential = indexes == list(range(1, expected_count + 1))
        if not is_sequential:
            failures.append(f"{config.strategic_market_id} drug_index sequence mismatch")
        print(f"    {config.strategic_market_id}: 1-{expected_count}, sequential={is_sequential}")

    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def phase4_json_validity(parquet_rows: list[dict[str, Any]], catalog_path: Path) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 4] JSON validity / shape")
    print("=" * 72)

    failures: list[str] = []
    metadata_catalog = load_column_metadata_catalog(catalog_path)
    valid_counts = Counter()
    raw_cells_lengths: dict[str, set[int]] = defaultdict(set)
    extra_key_sets: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    metadata_key_counts: dict[str, set[int]] = defaultdict(set)

    for row in parquet_rows:
        market_id = row["strategic_market_id"]
        parsed = {}
        for column in JSON_COLUMNS:
            try:
                parsed[column] = json.loads(row[column])
                valid_counts[column] += 1
            except Exception as exc:
                failures.append(f"{_pk(row)} {column} JSON parse failed: {exc}")
                continue

        if "drug_extra_json" in parsed:
            extra_key_sets[market_id].add(tuple(sorted(parsed["drug_extra_json"].keys())))
        if "raw_row_json" in parsed:
            raw_payload = parsed["raw_row_json"]
            raw_cells_lengths[market_id].add(len(raw_payload.get("cells", [])))
            if str(raw_payload.get("source_row_id")) != str(row["source_row_id"]):
                failures.append(f"{_pk(row)} raw_row_json.source_row_id mismatch")
        if "column_metadata_json" in parsed:
            metadata = parsed["column_metadata_json"]
            metadata_key_counts[market_id].add(len(metadata))
            if metadata != metadata_catalog[market_id]:
                failures.append(f"{_pk(row)} column_metadata_json structure mismatch")

    print("  JSON valid counts:")
    for column in JSON_COLUMNS:
        print(f"    {column}: {valid_counts[column]}")

    print("\n  market JSON shape:")
    for config in MARKET_SHEETS:
        market_id = config.strategic_market_id
        metadata = metadata_catalog[market_id]
        expected_extra_keys = tuple(_expected_extra_keys(metadata))
        if extra_key_sets[market_id] != {expected_extra_keys}:
            failures.append(f"{market_id} drug_extra_json key set mismatch: {extra_key_sets[market_id]}")
        if raw_cells_lengths[market_id] != {26}:
            failures.append(f"{market_id} raw_row_json cells length mismatch: {raw_cells_lengths[market_id]}")
        if metadata_key_counts[market_id] != {len(metadata)}:
            failures.append(f"{market_id} column_metadata_json key count mismatch: {metadata_key_counts[market_id]}")
        print(
            f"    {market_id}: extras={list(expected_extra_keys) or '없음'}, "
            f"raw_cells={sorted(raw_cells_lengths[market_id])}, "
            f"metadata_keys={sorted(metadata_key_counts[market_id])}"
        )

    if failures:
        for failure in failures[:20]:
            print(f"  FAIL {failure}")
        return 0, len(failures)
    print("  result: PASS")
    return 1, 0


def phase5_schema(parquet_rows: list[dict[str, Any]], parquet_schema: Any) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 5] 31 DDL columns / dtype / helper columns")
    print("=" * 72)

    failures: list[str] = []
    schema_names = tuple(parquet_schema.names)
    if schema_names != MASTER_DRUG_COLUMNS:
        failures.append(f"schema column mismatch: {schema_names}")

    for field in parquet_schema:
        if str(field.type) != "string":
            failures.append(f"column dtype is not string: {field.name}={field.type}")

    forbidden = {"source_files", "period"}
    present_forbidden = forbidden & set(schema_names)
    if present_forbidden:
        failures.append(f"helper columns present: {sorted(present_forbidden)}")

    bad_ingested_at = []
    for index, row in enumerate(parquet_rows, start=1):
        columns = tuple(row.keys())
        if columns != MASTER_DRUG_COLUMNS:
            failures.append(f"row {index} schema mismatch: {columns}")
            break
        ingested_at = row.get("ingested_at")
        if not isinstance(ingested_at, str) or not INGESTED_AT_RE.match(ingested_at):
            bad_ingested_at.append((_pk(row), ingested_at))
            if len(bad_ingested_at) >= 5:
                break
    if bad_ingested_at:
        failures.append(f"ingested_at format invalid examples: {bad_ingested_at}")

    print(f"  DDL columns:       {len(MASTER_DRUG_COLUMNS)}")
    print(f"  schema names:      {schema_names}")
    print("  column dtypes:")
    for field in parquet_schema:
        print(f"    {field.name}: {field.type}")
    print(f"  helper columns:    {sorted(present_forbidden) if present_forbidden else 'none'}")
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
        rows = conn.execute(f"SELECT {', '.join(MASTER_DRUG_COLUMNS)} FROM stg_master_drug").fetchall()
    return [dict(row) for row in rows]


def phase6_sqlite_corroboration(parquet_rows: list[dict[str, Any]], sqlite_path: Path) -> tuple[int, int]:
    print()
    print("=" * 72)
    print("[Phase 6] SQLite 31-column full row corroboration")
    print("=" * 72)

    sqlite_rows = load_sqlite_rows(sqlite_path)
    parquet_by_pk = keyed_by_pk(parquet_rows)
    sqlite_by_pk = keyed_by_pk(sqlite_rows)
    parquet_keys = set(parquet_by_pk)
    sqlite_keys = set(sqlite_by_pk)
    failures: list[str] = []
    json_mode_totals = Counter()
    row_pass = 0
    row_fail = 0
    mismatch_examples = []

    sqlite_market_distribution = Counter(row["strategic_market_id"] for row in sqlite_rows)
    expected_market_distribution = {
        market_id: expected["staging_rows"] for market_id, expected in EXPECTED_MARKET_STATS.items()
    }

    if len(sqlite_rows) != EXPECTED_ROW_COUNT:
        failures.append(f"SQLite row count mismatch: {len(sqlite_rows)}")
    if dict(sorted(sqlite_market_distribution.items())) != expected_market_distribution:
        failures.append(f"SQLite market distribution mismatch: {dict(sorted(sqlite_market_distribution.items()))}")
    if sqlite_keys != parquet_keys:
        failures.append(
            f"SQLite/parquet PK set mismatch: "
            f"sqlite_only={len(sqlite_keys - parquet_keys)}, parquet_only={len(parquet_keys - sqlite_keys)}"
        )

    if not failures:
        for pk in sorted(parquet_keys):
            mismatches, modes = compare_row_values(sqlite_by_pk[pk], parquet_by_pk[pk])
            json_mode_totals.update(modes)
            if not mismatches:
                row_pass += 1
                continue
            row_fail += 1
            if len(mismatch_examples) < 5:
                mismatch_examples.append(
                    {
                        "pk": pk,
                        "columns": mismatches,
                        "sqlite": {column: sqlite_by_pk[pk].get(column) for column in mismatches},
                        "parquet": {column: parquet_by_pk[pk].get(column) for column in mismatches},
                    }
                )

    print(f"  SQLite path:        {sqlite_path}")
    print(f"  SQLite rows:        {len(sqlite_rows)}")
    print(f"  SQLite unique PK:   {len(sqlite_keys)}")
    print(f"  Parquet unique PK:  {len(parquet_keys)}")
    print(f"  full row pass:      {row_pass}")
    print(f"  full row fail:      {row_fail}")
    print(f"  JSON compare modes: {dict(json_mode_totals)}")
    print("\n  SQLite market distribution:")
    for config in MARKET_SHEETS:
        market_id = config.strategic_market_id
        print(f"    {market_id}: {sqlite_market_distribution.get(market_id, 0)}")

    if mismatch_examples:
        print("  mismatch examples:")
        for example in mismatch_examples:
            print(f"    {example}")
    if failures or row_fail:
        for failure in failures:
            print(f"  FAIL {failure}")
        return 0, len(failures) + row_fail
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
    print("Verify MI Master drug Parquet")
    print("=" * 72)
    print(f"  input file:   {input_file}")
    print(f"  catalog file: {catalog_path}")
    print(f"  parquet file: {parquet_file}")
    print(f"  sqlite file:  {sqlite_file}")

    expected_rows, _raw_stats = load_expected_rows(input_file, catalog_path)
    parquet_rows, parquet_schema = load_parquet_rows(parquet_file)

    phase_results = []
    phase_results.append(("Phase 0", *phase0_full_count(expected_rows, parquet_rows)))
    phase_results.append(("Phase 1", *phase1_stats(parquet_rows, catalog_path)))
    phase_results.append(("Phase 2", *phase2_full_row_compare(expected_rows, parquet_rows)))
    phase_results.append(("Phase 3", *phase3_pk_integrity(parquet_rows)))
    phase_results.append(("Phase 4", *phase4_json_validity(parquet_rows, catalog_path)))
    phase_results.append(("Phase 5", *phase5_schema(parquet_rows, parquet_schema)))
    phase_results.append(("Phase 6", *phase6_sqlite_corroboration(parquet_rows, sqlite_file)))

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
