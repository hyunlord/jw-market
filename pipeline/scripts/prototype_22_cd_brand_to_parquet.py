"""
prototype_22_cd_brand_to_parquet.py
===================================
Phase 14 Step 14-12 cd_brand -> Parquet.

Policy:
- D-49 candidate: cd-level serving tables are subsets of the ML-level serving
  tables. cd_brand is strategic_brand filtered to cd_id IS NOT NULL.
- Hybrid validation: use the existing Q-51 assignment logic as the source of
  truth and reapply the cd_filter conditions as a cross-check before writing.
- cd_id is a FK to cd_market.cd_id. cd_filter conditions are reached through
  cd_market.cd_filter_id.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pyarrow --break-system-packages")


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STRATEGIC_BRAND_FILE = Path("output/catalog/strategic_brand/strategic_brand.parquet")
DEFAULT_CD_MARKET_FILE = Path("output/catalog/cd_market/cd_market.parquet")
DEFAULT_CD_FILTER_FILE = Path("output/catalog/cd_filter/cd_filter.parquet")
DEFAULT_OUTPUT_FILE = Path("output/catalog/cd_brand/cd_brand.parquet")
STRATEGIC_BRAND_SCRIPT = Path("scripts/prototype_20_strategic_brand_to_parquet.py")
STRATEGIC_PRODUCT_SCRIPT = Path("scripts/prototype_21_strategic_product_to_parquet.py")

EXPECTED_ROW_COUNT = 2379


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def recompute_cd_assignments(
    brand_rows: list[dict[str, Any]],
    cd_market_rows: list[dict[str, Any]],
    cd_filter_rows: list[dict[str, Any]],
    contexts: dict[str, dict[str, Any]],
    brand_helpers: Any,
) -> list[dict[str, Any]]:
    filter_by_id = {str(row["cd_filter_id"]): row for row in cd_filter_rows}
    cd_markets_for_ml: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cd_market_rows:
        cd_markets_for_ml[str(row["ml_id"])].append(row)

    mismatches: list[dict[str, Any]] = []
    for row in brand_rows:
        brand_id = str(row["brand_id"])
        context = contexts.get(brand_id)
        if context is None:
            mismatches.append(
                {
                    "brand_id": brand_id,
                    "actual_cd_id": row.get("cd_id"),
                    "recomputed_cd_id": None,
                    "candidates": "",
                    "reason": "missing_source_context",
                }
            )
            continue

        match_context = {
            "ml_id": row["ml_id"],
            "atc4_code": context.get("atc4_code"),
            "class": row.get("class"),
            "molecule": row.get("molecule"),
            "dosage_form": row.get("dosage_form"),
            "nhi_type": row.get("nhi_type"),
        }
        recomputed_cd_id, candidates = brand_helpers.assign_cd_id(
            match_context,
            cd_markets_for_ml,
            filter_by_id,
        )
        actual_cd_id = row.get("cd_id")
        if actual_cd_id != recomputed_cd_id or len(candidates) > 1:
            mismatches.append(
                {
                    "brand_id": brand_id,
                    "actual_cd_id": actual_cd_id,
                    "recomputed_cd_id": recomputed_cd_id,
                    "candidates": ",".join(candidates),
                    "reason": "q51_vs_cd_filter_mismatch",
                }
            )
    return mismatches


def load_cd_brand_records(
    strategic_brand_path: Path,
    cd_market_path: Path,
    cd_filter_path: Path,
    ingested_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    brand_helpers = import_module(STRATEGIC_BRAND_SCRIPT, "prototype_20_helpers_for_cd_brand")
    product_helpers = import_module(STRATEGIC_PRODUCT_SCRIPT, "prototype_21_helpers_for_cd_brand")

    brand_rows = read_parquet_rows(strategic_brand_path)
    cd_market_rows = read_parquet_rows(cd_market_path)
    cd_filter_rows = read_parquet_rows(cd_filter_path)
    contexts = product_helpers.load_context_by_brand_id()

    mismatches = recompute_cd_assignments(
        brand_rows,
        cd_market_rows,
        cd_filter_rows,
        contexts,
        brand_helpers,
    )
    if mismatches:
        raise ValueError(f"Q-51 vs cd_filter cross-check mismatch: {mismatches[:10]}")

    timestamp = ingested_at or utc_now_datetime()
    cd_brand_rows = []
    for row in brand_rows:
        if row.get("cd_id") is None:
            continue
        out = {column: row.get(column) for column in brand_helpers.EXPECTED_COLUMNS}
        out["ingested_at"] = timestamp
        cd_brand_rows.append(out)

    validate_records(cd_brand_rows, brand_helpers, cd_market_rows)
    return cd_brand_rows, mismatches


def validate_records(
    records: list[dict[str, Any]],
    brand_helpers: Any,
    cd_market_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"cd_brand row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    expected_columns = tuple(brand_helpers.EXPECTED_COLUMNS)
    cd_ids = {str(row["cd_id"]) for row in cd_market_rows}
    brand_ids = [str(row["brand_id"]) for row in records]
    if len(set(brand_ids)) != len(brand_ids):
        raise ValueError("cd_brand.brand_id must be unique")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != expected_columns:
            raise ValueError(
                f"row {index} columns mismatch: expected={expected_columns}, actual={tuple(record.keys())}"
            )
        if record["cd_id"] is None:
            raise ValueError(f"row {index} cd_id must be non-null")
        if str(record["cd_id"]) not in cd_ids:
            raise ValueError(f"row {index} missing cd_market FK: {record['cd_id']}")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"row {index} ingested_at must be datetime")


def write_parquet(records: list[dict[str, Any]], output_file: Path, brand_helpers: Any) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=brand_helpers.STRATEGIC_BRAND_SCHEMA)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def validate_written_parquet(output_file: Path, brand_helpers: Any) -> None:
    table = pq.read_table(output_file)
    if table.schema != brand_helpers.STRATEGIC_BRAND_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={brand_helpers.STRATEGIC_BRAND_SCHEMA}\nactual={table.schema}")
    if table.num_rows != EXPECTED_ROW_COUNT:
        raise ValueError(f"written row count mismatch: {table.num_rows}")


def print_summary(records: list[dict[str, Any]], output_file: Path) -> None:
    counts: dict[str, int] = defaultdict(int)
    for row in records:
        counts[str(row["cd_id"])] += 1
    print("prototype Phase 14 Step 14-12 cd_brand -> Parquet")
    print(f"rows={len(records)}")
    print("columns=16")
    print(f"output={output_file}")
    print("cd_brand_distribution:")
    for cd_id in sorted(counts):
        print(f"  {cd_id}: {counts[cd_id]}")
    print("q51_cd_filter_cross_check: PASS")
    print("validate_records: PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 14 cd_brand parquet.")
    parser.add_argument("--strategic-brand", type=Path, default=DEFAULT_STRATEGIC_BRAND_FILE)
    parser.add_argument("--cd-market", type=Path, default=DEFAULT_CD_MARKET_FILE)
    parser.add_argument("--cd-filter", type=Path, default=DEFAULT_CD_FILTER_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    brand_helpers = import_module(STRATEGIC_BRAND_SCRIPT, "prototype_20_helpers_for_cd_brand_write")
    records, _ = load_cd_brand_records(
        args.strategic_brand,
        args.cd_market,
        args.cd_filter,
    )
    write_parquet(records, args.output, brand_helpers)
    validate_written_parquet(args.output, brand_helpers)
    print_summary(records, args.output)


if __name__ == "__main__":
    main()
