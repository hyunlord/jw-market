from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.etl.io.catalog.raw_sources import resolve_iqvia_latest, resolve_ubist_latest
from pipeline.etl.io.catalog.target_priority_records import load_dim_market_target_priority_records
from pipeline.etl.io.catalog.target_priority_schema import (
    AUTO_FILL_CACHE_COLUMNS,
    DEFAULT_CACHE_FILE,
    DEFAULT_DIM_COMPETITIVE_FILE,
    DEFAULT_IQVIA_DIR,
    DEFAULT_MASTER_DRUG_FILE,
    DEFAULT_OUTPUT_FILE,
    DEFAULT_SKELETON_FILE,
    DEFAULT_UBIST_BASE_DIR,
    DIM_MARKET_TARGET_PRIORITY_COLUMNS,
)


def write_cache(cache_rows: list[dict[str, str | None]], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(cache_rows, columns=AUTO_FILL_CACHE_COLUMNS).to_csv(cache_path, index=False)


def write_parquet(records: list[dict[str, str | None]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([(column, pa.string()) for column in DIM_MARKET_TARGET_PRIORITY_COLUMNS])
    arrays = [
        pa.array([record.get(column) for record in records], type=pa.string())
        for column in DIM_MARKET_TARGET_PRIORITY_COLUMNS
    ]
    table = pa.Table.from_arrays(arrays, schema=schema)
    pq.write_table(table, output_path, compression="snappy")


def print_summary(records: list[dict[str, str | None]], cache_path: Path, output_path: Path) -> None:
    source_view_counts = Counter(record["source_view"] for record in records)
    source_type_counts = Counter(record["source_type"] for record in records)
    auto_fill_null_count = sum(
        1
        for record in records
        if record["source_type"] == "auto_fill_top_n_by_sales" and record["target_customer"] is None
    )
    print("Phase 12 Round 6 dim_market_target_priority load complete")
    print(f"- rows: {len(records)}")
    print(f"- source_view: {dict(source_view_counts)}")
    print(f"- source_type: {dict(source_type_counts)}")
    print(f"- auto_fill rows without available latest-partition rank: {auto_fill_null_count}")
    print(f"- parquet: {output_path}")
    print(f"- auto_fill dictionary cache: {cache_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skeleton", type=Path, default=DEFAULT_SKELETON_FILE)
    parser.add_argument("--dim-competitive", type=Path, default=DEFAULT_DIM_COMPETITIVE_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--ubist", "--ubist-path", dest="ubist", type=Path, default=None)
    parser.add_argument("--iqvia", "--iqvia-path", dest="iqvia", type=Path, default=None)
    parser.add_argument("--ubist-base-dir", type=Path, default=DEFAULT_UBIST_BASE_DIR)
    parser.add_argument("--iqvia-dir", type=Path, default=DEFAULT_IQVIA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_FILE)
    parser.add_argument("--ingested-at", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ubist_path = args.ubist or resolve_ubist_latest(args.ubist_base_dir)
    iqvia_path = args.iqvia or resolve_iqvia_latest(args.iqvia_dir)
    records = load_dim_market_target_priority_records(
        skeleton_path=args.skeleton,
        dim_competitive_path=args.dim_competitive,
        master_drug_path=args.master_drug,
        ubist_path=ubist_path,
        iqvia_path=iqvia_path,
        cache_path=args.cache,
        ingested_at=args.ingested_at,
    )
    write_parquet(records, args.output)
    print_summary(records, args.cache, args.output)


if __name__ == "__main__":
    main()
