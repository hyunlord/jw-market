"""MI Master manual mapping table -> Parquet (prototype_10 facade)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._lib.common import write_records_parquet
from pipeline.etl.io.catalog.master.mapping_table_records import (
    load_column_metadata_catalog,
    load_mapping_records,
    resolve_input_file,
)
from pipeline.etl.io.catalog.master.mapping_table_schema import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    MASTER_MAPPING_TABLE_COLUMNS,
    MarketMappingStats,
)
from pipeline.etl.io.catalog.master.mapping_table_validation import _count_by, validate_records


def print_summary(
    records: list[dict[str, Any]],
    stats: list[MarketMappingStats],
    output_file: Path,
) -> None:
    print("Phase 09d master_mapping_table load")
    print(f"mapping_rows: {len(records)}")
    print(f"unique_mapping_id: {len({record['mapping_id'] for record in records})}")
    print(f"mapping_type_distribution: {_count_by(records, 'mapping_type')}")
    print("market_distribution:")
    for item in stats:
        print(
            f"  {item.strategic_market_id} {item.sheet_name}: "
            f"raw={item.raw_rows_scanned}, empty={item.empty_rows}, "
            f"excluded={item.excluded_rows}, staging={item.staging_rows}, "
            f"manual_specs={item.manual_specs}, mapping={item.mapping_rows}"
        )
    if output_file.exists():
        print(f"output_file: {output_file} ({output_file.stat().st_size:,} bytes)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load MI Master mapping_table parquet.")
    parser.add_argument("--input-file", type=Path, default=DEFAULT_INPUT_FILE)
    parser.add_argument("--catalog-path", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    xlsx_path = resolve_input_file(args.input_file)
    records, stats = load_mapping_records(xlsx_path, args.catalog_path)
    validate_records(records, stats)
    write_parquet(records, args.output_file)
    print_summary(records, stats, args.output_file)
    print("validate_records: PASS")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(records, MASTER_MAPPING_TABLE_COLUMNS, output_file, stringify=True)


if __name__ == "__main__":
    main()
