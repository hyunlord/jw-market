from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.market.cd_filter_schema import (
    CD_FILTER_COLUMNS,
    CD_FILTER_SCHEMA,
    DEFAULT_MARKET_DEFINITION_FILE,
    DEFAULT_OUTPUT_FILE,
    EXPECTED_SOURCE_FILE_VERSION,
    FILTER_COLUMNS,
    ML_EQUALS_CD_FILTER_IDS,
)
from pipeline.etl.io.catalog.market.cd_filter_specs import raw_filter_records
from pipeline.etl.io.catalog.market.cd_filter_schema import count_non_null, validate_records
from pipeline.etl.io.catalog._lib.catalog_parquet import validate_written_schema, write_typed_parquet
from pipeline.etl.io.catalog._lib.catalog_text import read_parquet_rows, source_file_version as source_file_version_from_rows


def source_file_version(path: Path) -> str:
    return source_file_version_from_rows(read_parquet_rows(path), expected=EXPECTED_SOURCE_FILE_VERSION)


def load_cd_filter_records(
    market_definition_path: Path,
    ingested_at=None,
) -> list[dict[str, Any]]:
    from pipeline.etl.io.catalog._lib.catalog_text import utc_now_datetime
    version = source_file_version(market_definition_path)
    timestamp = ingested_at or utc_now_datetime()
    records = raw_filter_records(version, timestamp)
    validate_records(records)
    return records


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_typed_parquet(records, output_file, CD_FILTER_SCHEMA)


def validate_written_parquet(output_file: Path) -> None:
    validate_records(validate_written_schema(output_file, CD_FILTER_SCHEMA))

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 14 cd_filter parquet.")
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_cd_filter_records(args.market_definition)
    write_parquet(records, args.output)
    validate_written_parquet(args.output)

    print("prototype Phase 14 Step 14-3 cd_filter -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(CD_FILTER_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at'].isoformat(sep=' ', timespec='seconds')}")
    print("filter_non_null_counts:")
    for column in FILTER_COLUMNS:
        print(f"  {column}: {count_non_null(records, column)}")
    print("ml_equals_cd_filter_ids:")
    for filter_id in sorted(ML_EQUALS_CD_FILTER_IDS):
        print(f"  {filter_id}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
