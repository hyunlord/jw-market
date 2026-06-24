from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pipeline.etl.io.catalog._lib.catalog_parquet import write_typed_parquet
from pipeline.etl.io.catalog.brand.strategic_brand_logic import (
    assign_cd_id,
    cd_filter_conditions,
    field_matches,
    make_name,
    strategic_fields,
)
from pipeline.etl.io.catalog.brand.strategic_brand_records import load_strategic_brand_records
from pipeline.etl.io.catalog.brand.strategic_brand_schema import EXPECTED_COLUMNS, EXPECTED_ROW_COUNT, STRATEGIC_BRAND_SCHEMA
from pipeline.etl.io.catalog.brand.strategic_brand_schema import validate_records, write_gadrelet_cache


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_typed_parquet(records, output_file, STRATEGIC_BRAND_SCHEMA)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != STRATEGIC_BRAND_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={STRATEGIC_BRAND_SCHEMA}\nactual={table.schema}")
    if table.num_rows < EXPECTED_ROW_COUNT:
        raise ValueError(f"written row count below baseline {EXPECTED_ROW_COUNT}: {table.num_rows}")
