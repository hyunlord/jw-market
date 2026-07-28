from __future__ import annotations

import argparse
from contextlib import ExitStack
from decimal import Decimal
from itertools import chain
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .general_catalog import load_catalog_key_map
from .general_config import (
    ALLOWED_SOURCES,
    DRY_RUN_DIR,
    GENERAL_BRAND_INSERT_COLUMNS,
    GENERAL_MARKET_INSERT_COLUMNS,
    LOGGER,
    MEASURES_BY_SOURCE,
)
from .general_config import general_brand_jsonl_path, general_market_jsonl_path
from .general_db import (
    delete_source_rows,
    ensure_json_columns,
    insert_rows,
    replace_scoped_source_rows_from_jsonl,
    replace_source_rows_from_jsonl,
)
from .general_iqvia import iqvia_measure_frame, load_iqvia_base_frame
from .general_json import JsonlStreamSink, json_ready
from .general_rows import (
    build_brand_market_state,
    build_brand_period_summary,
    build_brand_rows,
    build_market_rows,
    iter_atc4_chunks,
)
from .general_ubist import (
    UbistAtc4Workset,
    iter_ubist_atc4_worksets,
    load_ubist_base_frame,
    ubist_measure_frame,
)


def _workset_parts(
    workset: UbistAtc4Workset | tuple[str, Any],
) -> tuple[str, Any]:
    if isinstance(workset, tuple):
        atc4_code, frame = workset
        return str(atc4_code), lambda: iter((frame,))
    return workset.atc4_code, workset.iter_frames


def compute_general(
    source: str,
    dry_run: bool = False,
    insert: bool = False,
    limit_atc4: int | None = None,
    max_rows: int | None = None,
    output_dir: Path | None = None,
    ml: str | None = None,
    spool_dir: Path | None = None,
    memory_budget_bytes: int | None = None,
    commit_each_batch: bool = False,
    atc4_scope: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    catalog_map = load_catalog_key_map()
    all_brand_rows: list[dict[str, Any]] = []
    all_market_rows: list[dict[str, Any]] = []
    measure_stats = {}
    raw_partitioned_ubist = (
        source == "ubist"
        and ml is None
        and os.environ.get("S4_INPUT_MODE", "raw") != "enriched"
    )
    if atc4_scope and not raw_partitioned_ubist:
        raise ValueError("ATC4-scoped mart recompute is supported for raw UBIST only")
    ubist_base = (
        load_ubist_base_frame(max_rows=max_rows, ml=ml)
        if source == "ubist" and not raw_partitioned_ubist
        else None
    )
    iqvia_base = load_iqvia_base_frame(max_rows=max_rows) if source == "iqvia_nsa" else None
    prepared_partitions = None
    if raw_partitioned_ubist:
        partition_kwargs: dict[str, Any] = {
            "max_rows": max_rows,
            "limit_atc4": limit_atc4,
            "spool_dir": spool_dir,
            "atc4_scope": atc4_scope,
        }
        if memory_budget_bytes is not None:
            partition_kwargs["memory_budget_bytes"] = memory_budget_bytes
        partition_iterator = iter_ubist_atc4_worksets(**partition_kwargs)
        first_partition = next(partition_iterator, None)
        if first_partition is None and insert:
            raise RuntimeError("no UBIST ATC4 partitions; existing mart rows were preserved")
        prepared_partitions = (
            chain((first_partition,), partition_iterator)
            if first_partition is not None
            else iter(())
        )
    if insert:
        ensure_json_columns(
            "mart_general_brand_metric",
            ("channel_specialty_matrix", "audit_code_matrix", "dimension_data", "dimension_channel_data"),
        )
    if insert and not raw_partitioned_ubist:
        delete_source_rows("mart_general_brand_metric", source)
        delete_source_rows("mart_general_market_metric", source)
    with ExitStack() as stack:
        stream_output_dir = output_dir
        if raw_partitioned_ubist and insert and not dry_run:
            stream_output_dir = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="ubist-mart-insert-")
                )
            )
        stream_rows = dry_run or (raw_partitioned_ubist and insert)
        brand_output_path = general_brand_jsonl_path(source, stream_output_dir)
        market_output_path = general_market_jsonl_path(source, stream_output_dir)
        brand_sink = (
            stack.enter_context(JsonlStreamSink(brand_output_path))
            if stream_rows
            else None
        )
        market_sink = (
            stack.enter_context(JsonlStreamSink(market_output_path))
            if stream_rows
            else None
        )

        if raw_partitioned_ubist:
            measure_stats = {
                measure: {"input_rows": 0, "brand_rows": 0, "market_rows": 0}
                for measure in MEASURES_BY_SOURCE[source]
            }
            assert prepared_partitions is not None
            for workset in prepared_partitions:
                atc4_code, frame_factory = _workset_parts(workset)
                measure_columns = (
                    ("sales", "raw_sales_minor"),
                    ("volume", "raw_volume_minor"),
                )
                input_rows_by_measure = {measure: 0 for measure, _ in measure_columns}
                summaries_by_measure = {measure: [] for measure, _ in measure_columns}
                for frame in frame_factory():
                    for measure, value_column in measure_columns:
                        input_rows_by_measure[measure] += int(
                            (frame[value_column].notna() & (frame[value_column] > 0)).sum()
                        )
                        summaries_by_measure[measure].append(
                            build_brand_period_summary(frame, value_column=value_column)
                        )
                market_states = {
                    measure: build_brand_market_state(
                        summaries,
                        value_column="raw_value_minor",
                        minor_unit_scale=Decimal("100"),
                    )
                    for measure, summaries in summaries_by_measure.items()
                }
                brand_rows_by_measure = {
                    measure: [] for measure, _ in measure_columns
                }
                for frame in frame_factory():
                    for measure, value_column in measure_columns:
                        brand_rows_by_measure[measure].extend(
                            build_brand_rows(
                                source,
                                measure,
                                frame,
                                catalog_map,
                                value_column=value_column,
                                market_state=market_states[measure],
                                minor_unit_scale=Decimal("100"),
                            )
                        )
                for measure, _value_column in measure_columns:
                    brand_rows = brand_rows_by_measure[measure]
                    brand_rows.sort(
                        key=lambda row: (row["brand_key"], row["atc4_code"])
                    )
                    market_rows = build_market_rows(source, measure, brand_rows)
                    input_rows = input_rows_by_measure[measure]
                    stats_for_measure = measure_stats[measure]
                    stats_for_measure["input_rows"] += input_rows
                    stats_for_measure["brand_rows"] += len(brand_rows)
                    stats_for_measure["market_rows"] += len(market_rows)
                    if brand_sink is not None and market_sink is not None:
                        brand_sink.write(brand_rows)
                        market_sink.write(market_rows)
                        if not all_brand_rows and brand_rows:
                            all_brand_rows.append(brand_rows[0])
                        if not all_market_rows and market_rows:
                            all_market_rows.append(market_rows[0])
                    LOGGER.info(
                        "[%s/%s/%s] input=%s brand_rows=%s market_rows=%s",
                        source,
                        measure,
                        atc4_code,
                        f"{input_rows:,}",
                        f"{len(brand_rows):,}",
                        f"{len(market_rows):,}",
                    )
                    del brand_rows, market_rows
            if insert:
                assert brand_sink is not None and market_sink is not None
                brand_sink.flush()
                market_sink.flush()
                replace_rows = (
                    replace_scoped_source_rows_from_jsonl
                    if atc4_scope
                    else replace_source_rows_from_jsonl
                )
                replace_kwargs: dict[str, Any] = {}
                if atc4_scope:
                    replace_kwargs["atc4_scope"] = atc4_scope
                replace_rows(
                    source=source,
                    brand_path=brand_output_path,
                    market_path=market_output_path,
                    brand_columns=GENERAL_BRAND_INSERT_COLUMNS,
                    market_columns=GENERAL_MARKET_INSERT_COLUMNS,
                    commit_each_batch=commit_each_batch,
                    **replace_kwargs,
                )
        else:
            for measure in MEASURES_BY_SOURCE[source]:
                frame = (
                    ubist_measure_frame(ubist_base, measure)
                    if source == "ubist"
                    else iqvia_measure_frame(iqvia_base, measure)
                )
                input_rows = 0
                brand_count = 0
                market_count = 0
                for atc4_code, chunk in iter_atc4_chunks(frame, limit_atc4):
                    input_rows += int(len(chunk))
                    brand_rows = build_brand_rows(source, measure, chunk, catalog_map)
                    market_rows = build_market_rows(source, measure, brand_rows)
                    brand_count += len(brand_rows)
                    market_count += len(market_rows)
                    if brand_sink is not None and market_sink is not None:
                        brand_sink.write(brand_rows)
                        market_sink.write(market_rows)
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
                measure_stats[measure] = {
                    "input_rows": input_rows,
                    "brand_rows": brand_count,
                    "market_rows": market_count,
                }
        for measure, measure_stat in measure_stats.items():
            LOGGER.info(
                "[%s/%s] input=%s brand_rows=%s market_rows=%s",
                source,
                measure,
                f"{measure_stat['input_rows']:,}",
                f"{measure_stat['brand_rows']:,}",
                f"{measure_stat['market_rows']:,}",
            )
    stats = {
        "source": source,
        "brand_rows": sum(item["brand_rows"] for item in measure_stats.values()),
        "market_rows": sum(item["market_rows"] for item in measure_stats.values()),
        "measures": measure_stats,
        "return_mode": "streamed_preview" if dry_run else "complete",
        "atc4_scope": list(atc4_scope or ()),
    }
    if dry_run:
        stats["output_paths"] = {
            "brand": str(brand_output_path),
            "market": str(market_output_path),
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
    parser.add_argument("--spool-dir", type=Path, default=None)
    parser.add_argument("--memory-budget-bytes", type=int, default=None)
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
            spool_dir=args.spool_dir,
            memory_budget_bytes=args.memory_budget_bytes,
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
