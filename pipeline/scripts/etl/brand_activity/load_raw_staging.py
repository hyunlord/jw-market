#!/usr/bin/env python3
# /// script
# dependencies = [
#   "openpyxl>=3.1.5",
#   "pymysql>=1.1.1",
# ]
# ///
# ─── How to run ───
# uv run --script pipeline/scripts/etl/brand_activity/load_raw_staging.py --execute --repeat 2
"""Load brand-activity raw staging and rebuild the isolated 3-year stage."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Final

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.brand_activity.csd_core import deduplicate_rows
from pipeline.scripts.etl.brand_activity.ingest_keyword import read_keyword_events
from pipeline.scripts.etl.brand_activity.ingest_meeting import read_meeting_events
from pipeline.scripts.etl.brand_activity.km_core import JsonValue, KeywordEvent, MeetingEvent, source_sha256
from pipeline.scripts.etl.brand_activity.raw_db import DbConfig, SourceRows, load_sources
from pipeline.scripts.etl.brand_activity.raw_extract import (
    CsdSourceRow,
    SourceRoots,
    read_csd_source_rows,
    resolve_source_roots,
)
from pipeline.scripts.etl.brand_activity.raw_source_sets import (
    CoverageSources,
    discover_combined_source_files,
    source_collection_by_file,
    source_collection_counts,
    target_market_coverage,
)
from pipeline.scripts.etl.brand_activity.raw_staging import recent_month_window


DEFAULT_SOURCE_ROOT: Final[Path] = ROOT / "data" / "IQVIA" / "CSD"
DEFAULT_LEGACY_SOURCE_ROOT: Final[Path] = ROOT / "data" / "IQVIA" / "CSD2"
DEFAULT_AUDIT_DIR: Final[Path] = ROOT / "audit" / "brand_activity_raw_staging"
RAW_SCHEMA: Final[str] = "jw_brand_activity_raw_stage"
STAGE_SCHEMA: Final[str] = "jw_brand_activity_stage"


def parse_args() -> argparse.Namespace:
    """Parse the local-only raw staging command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--legacy-source-root", type=Path, default=DEFAULT_LEGACY_SOURCE_ROOT)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3308)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-password-env", default="MARIADB_ROOT_PASSWORD")
    parser.add_argument("--raw-schema", default=RAW_SCHEMA)
    parser.add_argument("--stage-schema", default=STAGE_SCHEMA)
    return parser.parse_args()


def main() -> int:
    """Run source parsing, optional DB load, and audit evidence generation."""
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    roots = resolve_source_roots(args.source_root)
    files = discover_combined_source_files(roots, args.legacy_source_root)
    source_collections = source_collection_by_file(files)
    source_rows = read_all_sources(files)
    window = recent_month_window(max_period(source_rows))
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    source_manifest = build_source_manifest(files, source_collections)
    profile = build_profile(roots, files, source_rows, window, source_collections)
    write_json(args.audit_dir / "source_manifest.json", source_manifest)
    write_json(args.audit_dir / "source_profile.json", profile)
    load_runs: list[dict[str, JsonValue]] = []
    if args.execute:
        config = DbConfig(
            host=args.db_host,
            port=args.db_port,
            user=args.db_user,
            password=database_password(args),
            raw_schema=str(args.raw_schema),
            stage_schema=str(args.stage_schema),
        )
        for pass_index in range(1, args.repeat + 1):
            stats = load_sources(config, source_rows, window)
            load_runs.append({"pass": pass_index, **asdict(stats)})
    run_summary = {
        "execute": bool(args.execute),
        "repeat": args.repeat,
        "raw_schema": str(args.raw_schema),
        "stage_schema": str(args.stage_schema),
        "analysis_window": {"start": window[0], "end": window[1]},
        "load_runs": load_runs,
        "source_rows": {
            "csd": len(source_rows.csd),
            "keyword": len(source_rows.keyword),
            "meeting": len(source_rows.meeting),
        },
    }
    write_json(args.audit_dir / "run_summary.json", run_summary)
    print(json.dumps(run_summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def read_all_sources(files: dict[str, list[Path]]) -> SourceRows:
    """Parse all source workbooks into typed raw rows."""
    csd_rows: list[CsdSourceRow] = []
    for workbook in files["csd"]:
        csd_rows.extend(read_csd_source_rows(workbook, source_sha256(workbook)))
    keyword_events = [event for workbook in files["keyword"] for event in read_keyword_events(workbook)]
    meeting_events = [event for workbook in files["meeting"] for event in read_meeting_events(workbook)]
    return SourceRows(csd=csd_rows, keyword=keyword_events, meeting=meeting_events)


def max_period(rows: SourceRows) -> str:
    """Return the newest row-level period across all three datasets."""
    periods = [row.period_ym for row in rows.csd]
    periods.extend(event.period_ym for event in rows.keyword)
    periods.extend(event.period_ym for event in rows.meeting)
    return max(periods)


def build_source_manifest(files: dict[str, list[Path]], source_collections: dict[str, str]) -> list[dict[str, JsonValue]]:
    """Hash every source workbook used by the load."""
    return [
        {
            "dataset": dataset,
            "source_collection": source_collections.get(path.name, "unknown"),
            "path": str(path),
            "file": path.name,
            "sha256": source_sha256(path),
        }
        for dataset, paths in files.items()
        for path in paths
    ]


def build_profile(
    roots: SourceRoots,
    files: dict[str, list[Path]],
    rows: SourceRows,
    window: tuple[str, str],
    source_collections: dict[str, str],
) -> dict[str, JsonValue]:
    """Build redacted source audit facts and market coverage counts."""
    selected_csd = [row.to_stage_row() for row in rows.csd if row.selected_for_stage]
    deduped_csd, dedup_report = deduplicate_rows(selected_csd)
    product_markets = product_market_map(deduped_csd)
    return {
        "source_roots": {
            "csd": str(roots.csd),
            "keyword": sorted({str(path.parent) for path in files["keyword"]}),
            "meeting": sorted({str(path.parent) for path in files["meeting"]}),
        },
        "source_files": {dataset: [path.name for path in paths] for dataset, paths in files.items()},
        "source_collection_counts": source_collection_counts(files),
        "analysis_window": {"start": window[0], "end": window[1], "basis": "max row-level period"},
        "current_loader_audit": {
            "csd_transform": "market sheets excluding Market2 + Region == TOTAL + natural-grain dedup",
            "keyword_meeting_transform": "event rows preserved; DB loader truncates stage before insert",
        },
        "source_periods": {
            "csd": period_counter([row.period_ym for row in rows.csd]),
            "keyword": period_counter([event.period_ym for event in rows.keyword]),
            "meeting": period_counter([event.period_ym for event in rows.meeting]),
        },
        "source_year_rows": {
            "csd": year_counter([row.period_ym for row in rows.csd]),
            "keyword": year_counter([event.period_ym for event in rows.keyword]),
            "meeting": year_counter([event.period_ym for event in rows.meeting]),
        },
        "csd": {
            "raw_rows_all_regions_market_sheets": len(rows.csd),
            "stage_selected_total_rows": len(selected_csd),
            "deduped_stage_rows_all_periods": len(deduped_csd),
            "dedup_report": dedup_report,
            "market2_rows_preserved_raw": sum(1 for row in rows.csd if row.source_sheet.endswith("Market2")),
        },
        "keyword": event_profile(rows.keyword),
        "meeting": event_profile(rows.meeting),
        "target_market_coverage": target_market_coverage(
            CoverageSources(
                product_markets=product_markets,
                keyword_events=rows.keyword,
                meeting_events=rows.meeting,
                window=window,
                source_collection=source_collections,
            )
        ),
    }


def product_market_map(rows: list[object]) -> dict[str, set[str]]:
    """Map CSD master products to observed CSD markets."""
    mapping: dict[str, set[str]] = {}
    for row in rows:
        product = row.master_product
        mapping.setdefault(product, set()).add(row.market)
    return mapping


def event_profile(events: list[KeywordEvent] | list[MeetingEvent]) -> dict[str, JsonValue]:
    """Return row, file, period, ATC4, and product counts without raw text."""
    return {
        "rows": len(events),
        "periods": period_counter([event.period_ym for event in events]),
        "source_files": period_counter([event.source_file for event in events]),
        "therapeutic_class": period_counter([event.therapeutic_class for event in events]),
        "products": len({event.product_name for event in events}),
    }


def period_counter(values: list[str]) -> dict[str, int]:
    """Return sorted string frequency counts."""
    return dict(sorted(Counter(values).items()))


def year_counter(periods: list[str]) -> dict[str, int]:
    """Return row counts by period year."""
    return period_counter([period[:4] for period in periods])


def database_password(args: argparse.Namespace) -> str:
    """Resolve local MariaDB password without printing credentials."""
    if args.db_password:
        return str(args.db_password)
    env_value = os.environ.get(str(args.db_password_env))
    if env_value:
        return env_value
    return parse_env_file(ROOT / "pipeline" / "docker" / ".env").get(str(args.db_password_env), "")


def parse_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE dotenv content for local DB credentials."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_json(path: Path, payload: JsonValue) -> None:
    """Write deterministic UTF-8 JSON audit evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
