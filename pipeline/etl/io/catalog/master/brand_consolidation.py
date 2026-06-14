"""MI Master brand consolidation -> Parquet (prototype_09)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from pipeline.etl.io.catalog._lib.common import is_empty_row, utc_now_text, write_records_parquet
from pipeline.etl.io.catalog._lib.exclusion_policy import classify_exclusion_cells as classify_exclusion_cells_by_policy
from pipeline.etl.io.catalog.master.brand_consolidation_schema import (
    BRAND_GROUP_MEMBERS,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    HEADER_ROW,
    MASTER_BRAND_CONSOLIDATION_COLUMNS,
    PRODUCT_NAME_SOURCE_COLUMN,
    SOURCE_REMARK,
    SOURCE_SHEET,
    STRATEGIC_MARKET_ID,
    BrandConsolidationStats,
)
from pipeline.etl.io.catalog.master.brand_consolidation_validation import validate_records


def _is_class_header(header: Any) -> bool:
    text = str(header or "").strip().lower()
    normalized = "".join(ch for ch in text if ch.isalnum())
    return normalized in {"class", "class1", "class2"} or normalized.startswith("class")


def is_excluded_row(
    values: list[Any] | tuple[Any, ...],
    headers: list[Any] | tuple[Any, ...] | None = None,
    *,
    strategic_market_id: str | None = STRATEGIC_MARKET_ID,
    sheet_name: str | None = SOURCE_SHEET,
) -> bool:
    class_indexes = {idx for idx, header in enumerate(headers or []) if _is_class_header(header)}
    row_excluded, _class_excluded = classify_exclusion_cells_by_policy(
        values,
        class_indexes=class_indexes,
        strategic_market_id=strategic_market_id,
        sheet_name=sheet_name,
    )
    return row_excluded


def header_index(headers: list[Any], source_column: str) -> int:
    present_headers = {str(header).strip(): index for index, header in enumerate(headers) if header}
    if source_column not in present_headers:
        raise ValueError(f"required source column not found: {source_column}")
    return present_headers[source_column]


def build_brand_consolidation_rows(
    strategic_market_id: str,
    drug_record: dict[str, Any],
    ingested_at: str,
) -> list[dict[str, Any]]:
    groups = BRAND_GROUP_MEMBERS.get(strategic_market_id, {})
    product_name = drug_record.get("product_name")
    if product_name is None:
        return []
    product_text = str(product_name).strip()
    rows: list[dict[str, Any]] = []
    for brand_group, members in groups.items():
        if product_text in members:
            rows.append(
                {
                    "strategic_market_id": strategic_market_id,
                    "brand_group": brand_group,
                    "member_drug_index": drug_record["drug_index"],
                    "member_drug_name": product_text,
                    "source_remark": SOURCE_REMARK,
                    "source_sheet": drug_record["source_sheet"],
                    "source_file_version": drug_record["source_file_version"],
                    "ingested_at": ingested_at,
                }
            )
    return rows


def load_brand_consolidation_records(
    xlsx_path: Path,
    ingested_at: str | None = None,
) -> tuple[list[dict[str, Any]], BrandConsolidationStats]:
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        if SOURCE_SHEET not in wb.sheetnames:
            raise ValueError(f"required sheet not found: {SOURCE_SHEET!r}; sheets={wb.sheetnames}")

        ws = wb[SOURCE_SHEET]
        headers = list(next(ws.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW, values_only=True)))
        product_idx = header_index(headers, PRODUCT_NAME_SOURCE_COLUMN)
        timestamp = ingested_at or utc_now_text()
        stats = BrandConsolidationStats()
        records: list[dict[str, Any]] = []
        drug_index = 0

        for source_row_id, values in enumerate(
            ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True),
            start=HEADER_ROW + 1,
        ):
            stats.raw_rows_scanned += 1
            if is_empty_row(values):
                stats.empty_rows += 1
                continue
            if is_excluded_row(values, headers=headers):
                stats.excluded_rows += 1
                continue

            drug_index += 1
            stats.staging_drug_rows += 1
            drug_record = {
                "strategic_market_id": STRATEGIC_MARKET_ID,
                "drug_index": drug_index,
                "product_name": values[product_idx] if len(values) > product_idx else None,
                "source_sheet": SOURCE_SHEET,
                "source_file_version": xlsx_path.name,
                "source_row_id": source_row_id,
                "ingested_at": timestamp,
            }
            brand_rows = build_brand_consolidation_rows(STRATEGIC_MARKET_ID, drug_record, timestamp)
            records.extend(brand_rows)
            stats.brand_consolidation_rows += len(brand_rows)

        return records, stats
    finally:
        wb.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", default=str(DEFAULT_INPUT_FILE))
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT_FILE))
    args = parser.parse_args()

    input_file = Path(args.input_file)
    output_file = Path(args.output_file)
    if not input_file.exists():
        sys.exit(f"ERROR: input file not found: {input_file}")

    print("=" * 72)
    print("MI Master brand_consolidation -> Parquet")
    print("=" * 72)
    print(f"  input file:   {input_file}")
    print(f"  source sheet: {SOURCE_SHEET}")
    print(f"  output file:  {output_file}")
    print(f"  columns:      {len(MASTER_BRAND_CONSOLIDATION_COLUMNS)} DDL columns")
    print("  helpers:      none")

    records, stats = load_brand_consolidation_records(input_file)
    validate_records(records, stats)
    write_parquet(records, output_file)

    print("\nResult")
    print(f"  raw rows scanned:             {stats.raw_rows_scanned}")
    print(f"  empty rows:                   {stats.empty_rows}")
    print(f"  excluded rows:                {stats.excluded_rows}")
    print(f"  staging drug rows:            {stats.staging_drug_rows}")
    print(f"  brand consolidation rows:     {stats.brand_consolidation_rows}")
    print(f"  compound PK unique:           {len(records)}")
    print(f"  output size:                  {output_file.stat().st_size / 1024:.1f} KB")
    print(f"  ingested_at:                  {records[0]['ingested_at'] if records else None}")
    print("\nDone")


def write_parquet(records: list[dict[str, Any]], output_file: Path) -> None:
    write_records_parquet(records, MASTER_BRAND_CONSOLIDATION_COLUMNS, output_file, compression_level=3, stringify=True)


if __name__ == "__main__":
    main()
