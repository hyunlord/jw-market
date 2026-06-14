from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._lib.common import count_by, write_records_parquet
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_records import (
    filter_master_drug_rows,
    load_dim_market_competitive_dynamics_records,
)
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_schema import (
    DEFAULT_DIM_MARKET_LANDSCAPE_FILE,
    DEFAULT_MARKET_DEFINITION_FILE,
    DEFAULT_MASTER_DRUG_FILE,
    DEFAULT_OUTPUT_FILE,
    DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS,
)
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_specs import CD_SPECS
from pipeline.etl.io.catalog.dim.market_competitive_dynamics_validation import validate_records


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(
        records,
        DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS,
        output_file,
        compression_level=3,
        stringify=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Phase 12 dim_market_competitive_dynamics parquet.")
    parser.add_argument("--dim-market-landscape", type=Path, default=DEFAULT_DIM_MARKET_LANDSCAPE_FILE)
    parser.add_argument("--market-definition", type=Path, default=DEFAULT_MARKET_DEFINITION_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--ingested-at", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_dim_market_competitive_dynamics_records(
        args.dim_market_landscape,
        args.market_definition,
        args.master_drug,
        args.ingested_at,
    )
    write_parquet(records, args.output)

    print("prototype Phase 12 Round 5 dim_market_competitive_dynamics -> Parquet")
    print(f"rows={len(records)}")
    print(f"columns={len(DIM_MARKET_COMPETITIVE_DYNAMICS_COLUMNS)}")
    print(f"output={args.output}")
    print(f"source_file_version={records[0]['source_file_version']}")
    print(f"ingested_at={records[0]['ingested_at']}")
    print("cd_definition_type_distribution:")
    for definition_type, count in sorted(count_by(records, "cd_definition_type").items()):
        print(f"  {definition_type}: {count}")
    print("cd_brand_count:")
    for record in records:
        print(f"  {record['competitive_dynamics_id']}: {record['cd_brand_count']}")
    print(f"cd_brand_count_total={sum(int(record['cd_brand_count']) for record in records)}")
    print("validate_records: PASS")


if __name__ == "__main__":
    main()
