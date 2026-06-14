from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.market_catalog_parquet import validate_written_schema, write_typed_parquet
from pipeline.etl.io.catalog.market_catalog_text import read_parquet_rows
from pipeline.etl.io.catalog.ml_market_records import load_existing_ml_market_records, load_ml_market_records
from pipeline.etl.io.catalog.ml_market_schema import (
    DEFAULT_MARKET_DEFINITION_FILE,
    DEFAULT_MASTER_DRUG_FILE,
    DEFAULT_OUTPUT_FILE,
    ML_MARKET_COLUMNS,
    ML_MARKET_SCHEMA,
)
from pipeline.etl.io.catalog.ml_market_validation import count_true, nonnull_count, validate_records


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_typed_parquet(records, output_file, ML_MARKET_SCHEMA)


def validate_written_parquet(output_file: Path) -> None:
    validate_records(validate_written_schema(output_file, ML_MARKET_SCHEMA))


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
    from collections import Counter
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
        print(f"  {column}: {count_true(records, column)}")
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
        print(f"  {column}: {nonnull_count(records, column)}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
