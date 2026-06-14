from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.market_catalog_parquet import write_typed_parquet
from pipeline.etl.io.catalog.raw_sources import resolve_iqvia_latest, resolve_ubist_latest
from pipeline.etl.io.catalog.strategic_product_context import load_context_by_brand_id
from pipeline.etl.io.catalog.strategic_product_records import load_strategic_product_records
from pipeline.etl.io.catalog.strategic_product_schema import EXPECTED_COLUMNS, STRATEGIC_PRODUCT_SCHEMA
from pipeline.etl.io.catalog.strategic_product_validation import validate_records, write_coverage_cache


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_typed_parquet(records, output_file, STRATEGIC_PRODUCT_SCHEMA)


def validate_written_parquet(output_file: Path) -> None:
    table = pq.read_table(output_file)
    if table.schema != STRATEGIC_PRODUCT_SCHEMA:
        raise ValueError(f"written schema mismatch:\nexpected={STRATEGIC_PRODUCT_SCHEMA}\nactual={table.schema}")
