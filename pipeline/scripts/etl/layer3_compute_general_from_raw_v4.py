#!/usr/bin/env python3
"""Build/load general-view marts directly from Layer 1 raw data.

Phase 16-G-4-Fix-GeneralView.

Direction B:
  - strategic view path remains Layer 1 -> Layer 2 -> strategic marts.
  - general view path is Layer 1 raw -> mart_general_*.

Only mart_general_brand_metric and mart_general_market_metric are touched when
``--insert`` is used. Strategic marts, Layer 1, Layer 2, catalogs, migrations,
and response_store are not modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd

from brand_key_normalize import normalize_brand_name
from layer3_compute_general_v3 import (
    ALLOWED_SOURCES,
    GENERAL_BRAND_INSERT_COLUMNS,
    GENERAL_MARKET_INSERT_COLUMNS,
    MEASURES_BY_SOURCE,
    OUTPUT_DIR,
    PROJECT_ROOT,
    UBIST_GLOB,
    build_brand_rows,
    build_market_rows,
    insert_rows,
    json_ready,
    load_catalog_key_map,
    load_iqvia_base_frame,
    safe_float,
    ubist_channel_to_raw,
    ubist_specialty_to_raw,
    write_jsonl,
)
from ops_utils import configure_logging


LOGGER = configure_logging(__name__)
DRY_RUN_DIR = Path("/tmp/dryrun_generalview_v4")


def general_brand_jsonl_path(source: str, output_dir: Path | None = None) -> Path:
    return (output_dir or DRY_RUN_DIR) / f"general_v4_{source}_brand_rows.jsonl"


def general_market_jsonl_path(source: str, output_dir: Path | None = None) -> Path:
    return (output_dir or DRY_RUN_DIR) / f"general_v4_{source}_market_rows.jsonl"


def extract_atc4_sql(expr: str) -> str:
    return (
        "COALESCE(NULLIF(regexp_extract(upper(cast("
        f"{expr}"
        " as varchar)), '([A-Z][0-9A-Z]{2,5})', 1), ''), 'UNKNOWN')"
    )


def load_ubist_raw_base_frame(max_rows: int | None = None) -> pd.DataFrame:
    """Aggregate UBIST Layer 1 raw into the v3 brand-row input shape."""

    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    query = f"""
        WITH src AS (
          SELECT *
          FROM read_parquet('{UBIST_GLOB}')
          WHERE 브랜드 IS NOT NULL
            AND ATC IS NOT NULL
            AND (TRY_CAST(rx_amt AS DOUBLE) > 0 OR TRY_CAST(rx_qty AS DOUBLE) > 0)
          {limit}
        )
        SELECT
          CAST(브랜드 AS VARCHAR) AS brand_name,
          CAST(제품 AS VARCHAR) AS product_name,
          CAST(약품코드 AS VARCHAR) AS product_code,
          {extract_atc4_sql('ATC')} AS atc4_code,
          first(CAST(ATC AS VARCHAR)) AS atc4_desc,
          CAST(제조사 AS VARCHAR) AS manufacturer,
          CAST(판매사 AS VARCHAR) AS company,
          CAST(종별 AS VARCHAR) AS channel,
          CAST(진료과 AS VARCHAR) AS specialty,
          CAST(period_yyyymm AS VARCHAR) AS period_yyyymm,
          SUM(TRY_CAST(rx_amt AS DOUBLE)) AS raw_sales,
          SUM(TRY_CAST(rx_qty AS DOUBLE)) AS raw_volume,
          COUNT(*) AS source_row_count
        FROM src
        GROUP BY 1,2,3,4,6,7,8,9,10
    """
    LOGGER.info("[ubist/raw] aggregating Layer 1 UBIST parquet")
    con = duckdb.connect()
    try:
        frame = con.execute(query).df()
    finally:
        con.close()

    frame["source"] = "ubist"
    frame["brand_key"] = frame["brand_name"].map(normalize_brand_name)
    frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
    frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
    frame = frame.loc[(frame["brand_key"] != "") & (frame["atc4_code"] != "UNKNOWN")].copy()
    LOGGER.info(
        "[ubist/raw] aggregated rows=%s brand_keys=%s atc4=%s",
        f"{len(frame):,}",
        f"{frame['brand_key'].nunique():,}",
        f"{frame['atc4_code'].nunique():,}",
    )
    return frame


def load_iqvia_raw_base_frame(max_rows: int | None = None) -> pd.DataFrame:
    """Read IQVIA NSA Layer 1 raw using the existing raw parser."""

    frame = load_iqvia_base_frame(max_rows=max_rows)
    frame = frame.loc[(frame["brand_key"] != "") & (frame["atc4_code"] != "UNKNOWN")].copy()
    LOGGER.info(
        "[iqvia/raw] parsed rows=%s brand_keys=%s atc4=%s",
        f"{len(frame):,}",
        f"{frame['brand_key'].nunique():,}",
        f"{frame['atc4_code'].nunique():,}",
    )
    return frame


def measure_frame(base: pd.DataFrame, source: str, measure: str) -> pd.DataFrame:
    value_col = {
        ("ubist", "sales"): "raw_sales",
        ("ubist", "volume"): "raw_volume",
        ("iqvia_nsa", "sales"): "raw_sales",
        ("iqvia_nsa", "unit"): "raw_unit",
        ("iqvia_nsa", "dosage_unit"): "raw_dosage_unit",
        ("iqvia_nsa", "counting_unit"): "raw_counting_unit",
    }[(source, measure)]
    frame = base.copy()
    frame["measure"] = measure
    frame["raw_value"] = frame[value_col].map(safe_float)
    return frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()


def restrict_atc4(frame: pd.DataFrame, limit_atc4: int | None) -> pd.DataFrame:
    if not limit_atc4:
        return frame
    values = sorted(v for v in frame["atc4_code"].dropna().unique().tolist() if v != "UNKNOWN")[:limit_atc4]
    return frame.loc[frame["atc4_code"].isin(values)].copy()


def stamp_rows(rows: list[dict[str, Any]], source: str) -> None:
    for row in rows:
        payload = row.setdefault("payload", {})
        payload["phase"] = "16-G-4-Fix-GeneralView"
        payload["etl_version"] = "v4.0-direction-b"
        payload["input_path"] = "layer1_raw"
        payload["source_policy"] = "general_view_raw_direct"
        payload["source"] = source


def delete_general_source(source: str) -> None:
    from layer3_compute_general_v3 import mariadb_connect

    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mart_general_market_metric WHERE source=%s", (source,))
            cur.execute("DELETE FROM mart_general_brand_metric WHERE source=%s", (source,))
    finally:
        conn.close()


def compute_general_from_raw(
    source: str,
    dry_run: bool,
    insert: bool,
    output_dir: Path,
    limit_atc4: int | None = None,
    max_rows: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    if not dry_run and not insert:
        dry_run = True

    catalog_map = load_catalog_key_map()
    base = load_ubist_raw_base_frame(max_rows=max_rows) if source == "ubist" else load_iqvia_raw_base_frame(max_rows=max_rows)
    all_brand_rows: list[dict[str, Any]] = []
    all_market_rows: list[dict[str, Any]] = []
    measure_stats: dict[str, Any] = {}

    for measure in MEASURES_BY_SOURCE[source]:
        frame = restrict_atc4(measure_frame(base, source, measure), limit_atc4)
        brand_rows = build_brand_rows(source, measure, frame, catalog_map)
        stamp_rows(brand_rows, source)
        market_rows = build_market_rows(source, measure, brand_rows)
        stamp_rows(market_rows, source)
        all_brand_rows.extend(brand_rows)
        all_market_rows.extend(market_rows)
        measure_stats[measure] = {
            "input_rows": int(len(frame)),
            "brand_rows": len(brand_rows),
            "market_rows": len(market_rows),
            "distinct_brand_key": int(frame["brand_key"].nunique()) if not frame.empty else 0,
            "distinct_atc4": int(frame["atc4_code"].nunique()) if not frame.empty else 0,
        }
        LOGGER.info(
            "[%s/%s/raw] input=%s brand_rows=%s market_rows=%s",
            source,
            measure,
            f"{len(frame):,}",
            f"{len(brand_rows):,}",
            f"{len(market_rows):,}",
        )

    if dry_run:
        write_jsonl(general_brand_jsonl_path(source, output_dir), all_brand_rows)
        write_jsonl(general_market_jsonl_path(source, output_dir), all_market_rows)
    if insert:
        LOGGER.info("[%s/raw] deleting existing general mart rows for source", source)
        delete_general_source(source)
        insert_rows("mart_general_brand_metric", GENERAL_BRAND_INSERT_COLUMNS, all_brand_rows)
        insert_rows("mart_general_market_metric", GENERAL_MARKET_INSERT_COLUMNS, all_market_rows)

    stats = {
        "source": source,
        "input": "layer1_raw",
        "brand_rows": len(all_brand_rows),
        "market_rows": len(all_market_rows),
        "distinct_brand_key": len({row["brand_key"] for row in all_brand_rows}),
        "distinct_atc4": len({row["atc4_code"] for row in all_brand_rows}),
        "measures": measure_stats,
    }
    return all_brand_rows, all_market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=[*ALLOWED_SOURCES, "both"], default="both")
    parser.add_argument("--insert", action="store_true", help="Write to mart_general_*; default is dry-run only")
    parser.add_argument("--dry-run", action="store_true", help="Write JSONL dry-run artifacts")
    parser.add_argument("--output-dir", type=Path, default=DRY_RUN_DIR)
    parser.add_argument("--limit-atc4", type=int, default=None, help="Fast validation: process only first N ATC4 codes")
    parser.add_argument("--max-rows", type=int, default=None, help="Fast validation: raw row limit before aggregation")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = list(ALLOWED_SOURCES) if args.source == "both" else [args.source]
    dry_run = args.dry_run or not args.insert
    for source in sources:
        brand_rows, market_rows, stats = compute_general_from_raw(
            source=source,
            dry_run=dry_run,
            insert=args.insert,
            output_dir=args.output_dir,
            limit_atc4=args.limit_atc4,
            max_rows=args.max_rows,
        )
        print(f"\n=== {source} general v4 raw ===")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if brand_rows:
            print("sample brand row:")
            print(json.dumps(json_ready(brand_rows[0]), ensure_ascii=False)[:1200])
        if market_rows:
            print("sample market row:")
            print(json.dumps(json_ready(market_rows[0]), ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
