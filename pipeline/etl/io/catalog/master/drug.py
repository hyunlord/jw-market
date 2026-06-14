"""MI Master drug rows -> Parquet (prototype_11 facade)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._lib.common import (
    apply_column_mapping,
    explicit_lookup_join,
    is_empty_row,
    write_records_parquet,
)
from pipeline.etl.io.catalog.master.drug_records import (
    _headers_from_sheet,
    load_column_metadata_catalog,
    load_drug_records,
    resolve_input_file,
)
from pipeline.etl.io.catalog.master.drug_schema import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    MARKET_SHEETS,
    MASTER_DRUG_COLUMNS,
    MarketDrugStats,
)
from pipeline.etl.io.catalog.master.drug_validation import _count_by, validate_records


def print_summary(records: list[dict[str, Any]], stats: list[MarketDrugStats], output_file: Path) -> None:
    print("Phase 09e master_drug load")
    print(f"master_drug_rows: {len(records)}")
    print(f"compound_pk_unique: {len({(record['strategic_market_id'], str(record['drug_index'])) for record in records})}")
    print(f"source_type_distribution: {_count_by(records, 'source_type')}")
    print("market_distribution:")
    for item in stats:
        print(
            f"  {item.strategic_market_id} {item.sheet_name}: "
            f"raw={item.raw_rows_scanned}, empty={item.empty_rows}, "
            f"excluded={item.excluded_rows}, staging={item.staging_rows}"
        )
    if records:
        print(f"ingested_at: {records[0]['ingested_at']}")
    if output_file.exists():
        print(f"output_file: {output_file} ({output_file.stat().st_size:,} bytes)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load MI Master drug parquet.")
    parser.add_argument("--input-file", type=Path, default=DEFAULT_INPUT_FILE)
    parser.add_argument("--catalog-path", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    xlsx_path = resolve_input_file(args.input_file)
    records, stats = load_drug_records(xlsx_path, args.catalog_path)
    validate_records(records, stats, args.catalog_path)
    write_parquet(records, args.output_file)
    print_summary(records, stats, args.output_file)
    print("validate_records: PASS")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(records, MASTER_DRUG_COLUMNS, output_file, stringify=True)


if __name__ == "__main__":
    main()
