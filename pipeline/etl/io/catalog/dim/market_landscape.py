from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._lib.common import count_by, write_records_parquet
from pipeline.etl.io.catalog.dim.market_landscape_records import load_dim_market_landscape_records
from pipeline.etl.io.catalog.dim.market_landscape_schema import (
    DEFAULT_MARKET_DEFINITION_FILE,
    DEFAULT_MASTER_DRUG_FILE,
    DEFAULT_OUTPUT_FILE,
    DIM_MARKET_LANDSCAPE_COLUMNS,
)
from pipeline.etl.io.catalog.dim.market_landscape_validation import validate_records


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(
        records,
        DIM_MARKET_LANDSCAPE_COLUMNS,
        output_file,
        compression_level=3,
        stringify=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 12 dim_market_landscape parquet.")
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--ingested-at", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_dim_market_landscape_records(args.market_definition, args.master_drug, args.ingested_at)
    write_parquet(records, args.output)

    print("prototype Phase 12 Round 4 dim_market_landscape -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(DIM_MARKET_LANDSCAPE_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at']}")
    print("ml_definition_type_distribution:")
    for definition_type, count in sorted(count_by(records, "ml_definition_type").items()):
        print(f"  {definition_type}: {count}")
    print("ml_brand_count_by_market:")
    for record in records:
        print(f"  {record['strategic_market_id']}: {record['ml_brand_count']}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
