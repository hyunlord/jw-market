from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .general_catalog import load_catalog_key_map
from .general_config import ALLOWED_SOURCES, GENERAL_BRAND_INSERT_COLUMNS, GENERAL_MARKET_INSERT_COLUMNS, LOGGER, MEASURES_BY_SOURCE
from .general_config import general_brand_jsonl_path, general_market_jsonl_path
from .general_db import delete_source_rows, ensure_json_columns, insert_rows
from .general_iqvia import iqvia_measure_frame, load_iqvia_base_frame
from .general_json import json_ready, write_jsonl
from .general_rows import build_brand_rows, build_market_rows, iter_atc4_chunks
from .general_ubist import load_ubist_base_frame, ubist_measure_frame

def compute_general(
    source: str,
    dry_run: bool = False,
    insert: bool = False,
    limit_atc4: int | None = None,
    max_rows: int | None = None,
    output_dir: Path | None = None,
    ml: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    catalog_map = load_catalog_key_map()
    all_brand_rows: list[dict[str, Any]] = []
    all_market_rows: list[dict[str, Any]] = []
    measure_stats = {}
    ubist_base = load_ubist_base_frame(max_rows=max_rows, ml=ml) if source == "ubist" else None
    iqvia_base = load_iqvia_base_frame(max_rows=max_rows) if source == "iqvia_nsa" else None
    if insert:
        ensure_json_columns(
            "mart_general_brand_metric",
            ("channel_specialty_matrix", "dimension_data", "dimension_channel_data"),
        )
        delete_source_rows("mart_general_brand_metric", source)
        delete_source_rows("mart_general_market_metric", source)
    for measure in MEASURES_BY_SOURCE[source]:
        frame = ubist_measure_frame(ubist_base, measure) if source == "ubist" else iqvia_measure_frame(iqvia_base, measure)
        input_rows = 0
        brand_count = 0
        market_count = 0
        for atc4_code, chunk in iter_atc4_chunks(frame, limit_atc4):
            input_rows += int(len(chunk))
            brand_rows = build_brand_rows(source, measure, chunk, catalog_map)
            market_rows = build_market_rows(source, measure, brand_rows)
            brand_count += len(brand_rows)
            market_count += len(market_rows)
            if dry_run:
                all_brand_rows.extend(brand_rows)
                all_market_rows.extend(market_rows)
            if insert:
                insert_rows("mart_general_brand_metric", GENERAL_BRAND_INSERT_COLUMNS, brand_rows)
                insert_rows("mart_general_market_metric", GENERAL_MARKET_INSERT_COLUMNS, market_rows)
            LOGGER.info(
                "[%s/%s/%s] input=%s brand_rows=%s market_rows=%s",
                source,
                measure,
                atc4_code,
                f"{len(chunk):,}",
                f"{len(brand_rows):,}",
                f"{len(market_rows):,}",
            )
            del chunk, brand_rows, market_rows
        measure_stats[measure] = {"input_rows": input_rows, "brand_rows": brand_count, "market_rows": market_count}
        LOGGER.info("[%s/%s] input=%s brand_rows=%s market_rows=%s", source, measure, f"{input_rows:,}", f"{brand_count:,}", f"{market_count:,}")
    if dry_run:
        write_jsonl(general_brand_jsonl_path(source, output_dir), all_brand_rows)
        write_jsonl(general_market_jsonl_path(source, output_dir), all_market_rows)
    stats = {
        "source": source,
        "brand_rows": sum(item["brand_rows"] for item in measure_stats.values()),
        "market_rows": sum(item["market_rows"] for item in measure_stats.values()),
        "measures": measure_stats,
    }
    return all_brand_rows, all_market_rows, stats

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=ALLOWED_SOURCES)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert", action="store_true")
    parser.add_argument("--limit-atc4", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=None, help="Optional raw-row limit for fast validation only")
    parser.add_argument("--output-dir", type=Path, default=DRY_RUN_DIR)
    parser.add_argument("--ml", help="Optional Layer 2 ml_id filter for fast UBIST validation")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    if args.all and args.source:
        raise SystemExit("--all and --source are mutually exclusive")
    if not args.all and not args.source:
        raise SystemExit("Provide --source SOURCE or --all")
    sources = list(ALLOWED_SOURCES) if args.all else [args.source]
    for source in sources:
        brand_rows, market_rows, stats = compute_general(
            source=source,
            dry_run=args.dry_run,
            insert=args.insert,
            limit_atc4=args.limit_atc4,
            max_rows=args.max_rows,
            output_dir=args.output_dir,
            ml=args.ml,
        )
        print(f"\n=== {source} general v3.1 ===")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if brand_rows:
            print("sample brand row:")
            print(json.dumps(json_ready(brand_rows[0]), ensure_ascii=False)[:1200])
        if market_rows:
            print("sample market row:")
            print(json.dumps(json_ready(market_rows[0]), ensure_ascii=False)[:1200])
    return 0
