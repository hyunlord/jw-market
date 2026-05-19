"""
prototype_23_cd_product_to_parquet.py
=====================================
Phase 14 Step 14-12 cd_product -> Parquet.

Policy:
- D-49 candidate: cd-level serving tables are subsets of ML-level serving
  tables. cd_product is strategic_product filtered to cd_id IS NOT NULL and
  constrained to brand_id values present in cd_brand.
- cd_product keeps the exact strategic_product physical schema.
"""

from __future__ import annotations

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
DEFAULT_STRATEGIC_PRODUCT_FILE = Path("parquet/strategic_product/strategic_product.parquet")
DEFAULT_CD_BRAND_FILE = Path("parquet/cd_brand/cd_brand.parquet")
DEFAULT_CD_MARKET_FILE = Path("parquet/cd_market/cd_market.parquet")
DEFAULT_OUTPUT_FILE = Path("parquet/cd_product/cd_product.parquet")
STRATEGIC_PRODUCT_SCRIPT = Path("scripts/prototype_21_strategic_product_to_parquet.py")


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


def load_cd_product_records(
    strategic_product_path: Path,
    cd_brand_path: Path,
    cd_market_path: Path,
    ingested_at: datetime | None = None,
) -> list[dict[str, Any]]:
    product_helpers = import_module(STRATEGIC_PRODUCT_SCRIPT, "prototype_21_helpers_for_cd_product")
    product_rows = read_parquet_rows(strategic_product_path)
    cd_brand_rows = read_parquet_rows(cd_brand_path)
    cd_market_rows = read_parquet_rows(cd_market_path)

    cd_brand_by_id = {str(row["brand_id"]): row for row in cd_brand_rows}
    cd_ids = {str(row["cd_id"]) for row in cd_market_rows}
    timestamp = ingested_at or utc_now_datetime()

    cd_product_rows: list[dict[str, Any]] = []
    dropped_non_null_cd_rows: list[dict[str, Any]] = []
    cd_mismatch_rows: list[dict[str, Any]] = []
    for row in product_rows:
        if row.get("cd_id") is None:
            continue
        brand_id = str(row["brand_id"])
        cd_brand = cd_brand_by_id.get(brand_id)
        if cd_brand is None:
            dropped_non_null_cd_rows.append(
                {
                    "product_id": row.get("product_id"),
                    "brand_id": brand_id,
                    "cd_id": row.get("cd_id"),
                }
            )
            continue
        if row.get("cd_id") != cd_brand.get("cd_id"):
            cd_mismatch_rows.append(
                {
                    "product_id": row.get("product_id"),
                    "brand_id": brand_id,
                    "product_cd_id": row.get("cd_id"),
                    "brand_cd_id": cd_brand.get("cd_id"),
                }
            )
            continue
        out = {column: row.get(column) for column in product_helpers.EXPECTED_COLUMNS}
        out["ingested_at"] = timestamp
        cd_product_rows.append(out)

    if dropped_non_null_cd_rows:
        raise ValueError(f"cd_product brand FK missing from cd_brand: {dropped_non_null_cd_rows[:10]}")
    if cd_mismatch_rows:
        raise ValueError(f"cd_product.cd_id vs cd_brand.cd_id mismatch: {cd_mismatch_rows[:10]}")

    validate_records(cd_product_rows, product_helpers, cd_brand_rows, cd_ids)
    return cd_product_rows


def validate_records(
    records: list[dict[str, Any]],
    product_helpers: Any,
    cd_brand_rows: list[dict[str, Any]],
    cd_ids: set[str],
) -> None:
    expected_columns = tuple(product_helpers.EXPECTED_COLUMNS)
    product_ids = [str(row["product_id"]) for row in records]
    if len(set(product_ids)) != len(product_ids):
        raise ValueError("cd_product.product_id must be unique")
    cd_brand_ids = {str(row["brand_id"]) for row in cd_brand_rows}
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != expected_columns:
            raise ValueError(
                f"row {index} columns mismatch: expected={expected_columns}, actual={tuple(record.keys())}"
            )
        if record["cd_id"] is None:
            raise ValueError(f"row {index} cd_id must be non-null")
        if str(record["cd_id"]) not in cd_ids:
            raise ValueError(f"row {index} missing cd_market FK: {record['cd_id']}")
        if str(record["brand_id"]) not in cd_brand_ids:
            raise ValueError(f"row {index} missing cd_brand FK: {record['brand_id']}")
        if not isinstance(record["ingested_at"], datetime):
            raise ValueError(f"row {index} ingested_at must be datetime")


def write_parquet(records: list[dict[str, Any]], output_file: Path, product_helpers: Any) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=product_helpers.STRATEGIC_PRODUCT_SCHEMA)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def validate_written_parquet(output_file: Path, product_helpers: Any) -> None:
    table = pq.read_table(output_file)
    if table.schema != product_helpers.STRATEGIC_PRODUCT_SCHEMA:
        raise ValueError(
            f"written schema mismatch:\nexpected={product_helpers.STRATEGIC_PRODUCT_SCHEMA}\nactual={table.schema}"
        )


def print_summary(records: list[dict[str, Any]], output_file: Path) -> None:
    counts: dict[str, int] = defaultdict(int)
    for row in records:
        counts[str(row["cd_id"])] += 1
    print("prototype Phase 14 Step 14-12 cd_product -> Parquet")
    print(f"rows={len(records)}")
    print("columns=17")
    print(f"output={output_file}")
    print("cd_product_distribution:")
    for cd_id in sorted(counts):
        print(f"  {cd_id}: {counts[cd_id]}")
    print("validate_records: PASS")


def main() -> None:
    product_helpers = import_module(STRATEGIC_PRODUCT_SCRIPT, "prototype_21_helpers_for_cd_product_write")
    records = load_cd_product_records(
        DEFAULT_STRATEGIC_PRODUCT_FILE,
        DEFAULT_CD_BRAND_FILE,
        DEFAULT_CD_MARKET_FILE,
    )
    write_parquet(records, DEFAULT_OUTPUT_FILE, product_helpers)
    validate_written_parquet(DEFAULT_OUTPUT_FILE, product_helpers)
    print_summary(records, DEFAULT_OUTPUT_FILE)


if __name__ == "__main__":
    main()
