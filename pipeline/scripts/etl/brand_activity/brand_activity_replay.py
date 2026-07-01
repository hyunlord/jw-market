#!/usr/bin/env python3
# /// script
# dependencies = [
#   "openpyxl>=3.1.5",
#   "pymysql>=1.1.1",
#   "rich>=13.0.0",
#   "typer>=0.12.0",
#   "httpx2[http2,brotli,zstd]",
# ]
# ///
# ─── How to run ───
# uv run --script pipeline/scripts/etl/brand_activity/brand_activity_replay.py --dry-run
"""Replay Brand Activity raw, stage, master, and topic pipelines in order."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import StrEnum
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Final, TypeAlias

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue  # noqa: E402
from pipeline.scripts.etl.brand_activity import load_raw_staging  # noqa: E402

if TYPE_CHECKING:
    from pipeline.scripts.etl.brand_activity.raw_db import DbConfig

MonthWindow: TypeAlias = tuple[str, str]
STAGES: Final = ("raw", "stage", "master", "topic")
DEFAULT_AUDIT_DIR: Final = REPO_ROOT / "audit" / "brand_activity_replay"
DEFAULT_XLSX_GLOB: Final = str(REPO_ROOT / "docs/reference/MI*Master*.xlsx")


class Stage(StrEnum):
    """Replay stages in dependency order."""

    RAW = "raw"
    STAGE = "stage"
    MASTER = "master"
    TOPIC = "topic"


@dataclass(frozen=True, slots=True)
class TopicOptions:
    """Bounded options delegated to auto_topic.run_pipeline."""

    max_real_calls: int = 86
    axis_per_brand: int = 3
    axis_rows_cap: int = 240
    brand_rows: int = 5
    brands_per_market: int = 1
    large_market_limit: int = 0
    full_rows: bool = True
    axis_chunk_token_budget: int = 8000
    brand_batch_token_budget: int = 8000
    token_env: str = "GENOS_BEARER_TOKEN"


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    """Fully parsed replay options."""

    start: Stage
    only: Stage | None
    execute: bool
    save_to_db: bool
    raw_source: Path
    legacy_raw_source: Path
    xlsx: Path
    raw_schema: str
    stage_schema: str
    window: MonthWindow | None
    audit_dir: Path
    topic: TopicOptions


class ReplayError(RuntimeError):
    """Raised when replay cannot safely continue."""


def replay(options: ReplayOptions) -> dict[str, JsonValue]:
    """Run selected replay stages and return structured evidence."""
    plan = _planned_stages(options.start, options.only)
    results: dict[str, JsonValue] = {}
    for stage in plan:
        match stage:
            case Stage.RAW:
                results[stage.value] = _run_raw(options)
            case Stage.STAGE:
                results[stage.value] = _run_stage(options)
            case Stage.MASTER:
                results[stage.value] = _run_master(options)
            case Stage.TOPIC:
                results[stage.value] = _run_topic(options)
            case unreachable:
                raise AssertionError(f"unhandled stage: {unreachable}")
    return {
        "execute": options.execute,
        "save_to_db": options.save_to_db,
        "plan": [stage.value for stage in plan],
        "results": results,
    }


def _planned_stages(start: Stage, only: Stage | None) -> tuple[Stage, ...]:
    """Resolve ordered stage selection."""
    if only is not None:
        return (only,)
    ordered = tuple(Stage(value) for value in STAGES)
    return ordered[ordered.index(start) :]


def _run_raw(options: ReplayOptions) -> dict[str, JsonValue]:
    """Parse source workbooks and optionally load raw plus derived stage tables."""
    from pipeline.scripts.etl.brand_activity.raw_db import load_sources
    from pipeline.scripts.etl.brand_activity.raw_staging import recent_month_window

    roots = load_raw_staging.resolve_source_roots(options.raw_source)
    files = load_raw_staging.discover_combined_source_files(roots, options.legacy_raw_source)
    source_rows = load_raw_staging.read_all_sources(files)
    window = options.window or recent_month_window(load_raw_staging.max_period(source_rows))
    result: dict[str, JsonValue] = {
        "stage": "raw",
        "execute": options.execute,
        "raw_schema": options.raw_schema,
        "stage_schema": options.stage_schema,
        "analysis_window": {"start": window[0], "end": window[1]},
        "source_files": {dataset: len(paths) for dataset, paths in files.items()},
        "source_rows": {"csd": len(source_rows.csd), "keyword": len(source_rows.keyword)},
        "note": "execute uses raw_db.load_sources, which also refreshes derived stage tables transactionally",
    }
    if options.execute:
        stats = load_sources(_db_config(options), source_rows, window)
        result["load_stats"] = asdict(stats)
    return result


def _run_stage(options: ReplayOptions) -> dict[str, JsonValue]:
    """Optionally rebuild derived stage tables from existing raw tables."""
    from pipeline.scripts.etl.brand_activity.raw_stage_refresh import refresh_stage

    window = options.window or _raw_window(options)
    result: dict[str, JsonValue] = {
        "stage": "stage",
        "execute": options.execute,
        "raw_schema": options.raw_schema,
        "stage_schema": options.stage_schema,
        "analysis_window": {"start": window[0], "end": window[1]},
        "before_counts": _stage_counts(options),
    }
    if options.execute:
        import pymysql

        config = _db_config(options)
        connection = pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=8,
        )
        try:
            with connection.cursor() as cursor:
                result["refreshed_rows"] = refresh_stage(cursor, config.raw_schema, config.stage_schema, window)
            connection.commit()
        except pymysql.MySQLError:
            connection.rollback()
            raise
        finally:
            connection.close()
        result["after_counts"] = _stage_counts(options)
    return result


def _run_master(options: ReplayOptions) -> dict[str, JsonValue]:
    """Load or dry-run MI Master market-group staging tables."""
    from pipeline.scripts.etl.brand_activity.master_market_group_load import load as load_market_groups

    summary = load_market_groups(_resolve_xlsx(options.xlsx), schema=options.stage_schema, save=options.execute)
    return {
        "stage": "master",
        "execute": options.execute,
        "schema": summary.schema,
        "stg_master_market_definition": summary.market_definition,
        "stg_master_mapping_table": summary.mapping,
        "saved": summary.saved,
    }


def _run_topic(options: ReplayOptions) -> dict[str, JsonValue]:
    """Delegate topic extraction and mart upsert behavior to auto_topic."""
    tag = f"brand_activity_replay_{_timestamp_tag()}"
    command = [
        "uv",
        "run",
        "--script",
        str(REPO_ROOT / "pipeline/scripts/analysis/brand_activity/auto_topic/run_auto_topic.py"),
        "--execute" if options.execute else "--dry-run",
        "--save-to-db" if options.save_to_db else "--no-save-to-db",
        "--tag",
        tag,
        "--max-real-calls",
        str(options.topic.max_real_calls),
        "--axis-per-brand",
        str(options.topic.axis_per_brand),
        "--axis-rows-cap",
        str(options.topic.axis_rows_cap),
        "--brand-rows",
        str(options.topic.brand_rows),
        "--brands-per-market",
        str(options.topic.brands_per_market),
        "--large-market-limit",
        str(options.topic.large_market_limit),
        "--axis-chunk-token-budget",
        str(options.topic.axis_chunk_token_budget),
        "--brand-batch-token-budget",
        str(options.topic.brand_batch_token_budget),
        "--token-env",
        options.topic.token_env,
        "--docs-dir",
        str(options.audit_dir / "auto_topic_docs"),
        "--audit-dir",
        str(options.audit_dir / "auto_topic_audit"),
        "--stage-schema",
        options.stage_schema,
        "--full-rows" if options.topic.full_rows else "--capped-rows",
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise ReplayError(f"topic stage failed with exit={completed.returncode}: {completed.stderr.strip()}")
    return {
        "stage": "topic",
        "execute": options.execute,
        "save_to_db": options.save_to_db,
        "command": command,
        "summary": _parse_json_stdout(completed.stdout),
    }


def _parse_json_stdout(stdout: str) -> JsonValue:
    """Parse the final JSON object printed by run_auto_topic."""
    stripped = stdout.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    decoder = json.JSONDecoder()
    parsed: tuple[int, JsonValue] | None = None
    for offset, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            consumed = end
            if parsed is None or consumed > parsed[0]:
                parsed = (consumed, value)
    return parsed[1] if parsed is not None else {"raw_stdout_tail": stdout[-1000:]}


def _db_config(options: ReplayOptions) -> DbConfig:
    """Build a raw DB config from environment and validated schemas."""
    from pipeline.scripts.etl.brand_activity.raw_db import DbConfig, quote_stage_name

    env_values = load_raw_staging.parse_env_file(REPO_ROOT / "pipeline/docker/.env")
    password = os.environ.get("MARIADB_ROOT_PASSWORD") or env_values.get("MARIADB_ROOT_PASSWORD", "")
    if not password:
        raise ReplayError("MARIADB_ROOT_PASSWORD not set in environment or pipeline/docker/.env")
    return DbConfig(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", env_values.get("HOST_PORT", "3308"))),
        user=os.environ.get("MARIADB_USER", "root"),
        password=password,
        raw_schema=quote_stage_name(options.raw_schema, "jw_brand_activity_raw_stage"),
        stage_schema=quote_stage_name(options.stage_schema, "jw_brand_activity_stage"),
    )


def _raw_window(options: ReplayOptions) -> MonthWindow:
    """Infer the stage window from raw table max periods."""
    import pymysql
    from pipeline.scripts.etl.brand_activity.raw_staging import recent_month_window

    config = _db_config(options)
    connection = pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=8,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT MAX(period_ym) FROM (
                    SELECT period_ym FROM `{config.raw_schema}`.`raw_csd_channel_dynamics`
                    UNION ALL
                    SELECT period_ym FROM `{config.raw_schema}`.`raw_keyword_events`
                ) p
                """
            )
            max_period = cursor.fetchone()[0]
    finally:
        connection.close()
    if not max_period:
        raise ReplayError(f"no raw periods found in {config.raw_schema}")
    return recent_month_window(str(max_period))


def _stage_counts(options: ReplayOptions) -> dict[str, int]:
    """Read current derived stage row counts without modifying data."""
    import pymysql

    config = _db_config(options)
    connection = pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=8,
    )
    try:
        with connection.cursor() as cursor:
            return {
                table: _count_rows(cursor, config.stage_schema, table)
                for table in ("csd_channel_dynamics_stage", "km_keyword_event_stage")
            }
    finally:
        connection.close()


def _count_rows(cursor: object, schema: str, table: str) -> int:
    """Return a table count for replay evidence."""
    cursor.execute(f"SELECT COUNT(*) FROM `{schema}`.`{table}`")
    return int(cursor.fetchone()[0])


def _resolve_xlsx(path: Path) -> Path:
    """Resolve a literal or globbed MI Master workbook path."""
    if path.exists():
        return path
    matches = sorted(Path(match) for match in glob.glob(str(path)) if Path(match).is_file())
    if not matches:
        raise FileNotFoundError(f"MI Master workbook not found: {path}")
    return matches[0]


def _timestamp_tag() -> str:
    """Return a sortable local timestamp tag."""
    return time.strftime("%Y%m%d_%H%M%S")


def _parse_window(value: str) -> MonthWindow:
    """Parse a START,END month window."""
    try:
        start, end = value.split(",", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--window must be START,END") from exc
    return (start.strip(), end.strip())


def _parse_stage(value: str) -> Stage:
    """Parse a stage enum value for argparse."""
    try:
        return Stage(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"stage must be one of {', '.join(STAGES)}") from exc


def parse_args(argv: list[str] | None = None) -> ReplayOptions:
    """Parse CLI arguments into typed replay options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=_parse_stage, default=Stage.RAW, choices=tuple(Stage))
    parser.add_argument("--only", type=_parse_stage, choices=tuple(Stage), default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="execute", action="store_false", default=False)
    mode.add_argument("--execute", dest="execute", action="store_true")
    save = parser.add_mutually_exclusive_group()
    save.add_argument("--save-to-db", dest="save_to_db", action="store_true", default=False)
    save.add_argument("--no-save-to-db", dest="save_to_db", action="store_false")
    parser.add_argument("--raw-source", type=Path, default=load_raw_staging.DEFAULT_SOURCE_ROOT)
    parser.add_argument("--legacy-raw-source", type=Path, default=load_raw_staging.DEFAULT_LEGACY_SOURCE_ROOT)
    parser.add_argument("--xlsx", type=Path, default=Path(DEFAULT_XLSX_GLOB))
    parser.add_argument("--schema-raw", default="jw_brand_activity_raw_stage")
    parser.add_argument("--schema-stage", default="jw_brand_activity_stage")
    parser.add_argument("--window", type=_parse_window, default=None)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--brands-per-market", type=int, default=1)
    parser.add_argument("--brand-rows", type=int, default=5)
    parser.add_argument("--axis-per-brand", type=int, default=3)
    parser.add_argument("--large-market-limit", type=int, default=0)
    parser.add_argument("--max-real-calls", type=int, default=86)
    parser.add_argument("--token-env", default="GENOS_BEARER_TOKEN")
    args = parser.parse_args(argv)
    return ReplayOptions(
        start=args.start,
        only=args.only,
        execute=bool(args.execute),
        save_to_db=bool(args.save_to_db),
        raw_source=args.raw_source,
        legacy_raw_source=args.legacy_raw_source,
        xlsx=args.xlsx,
        raw_schema=str(args.schema_raw),
        stage_schema=str(args.schema_stage),
        window=args.window,
        audit_dir=args.audit_dir,
        topic=TopicOptions(
            max_real_calls=int(args.max_real_calls),
            axis_per_brand=int(args.axis_per_brand),
            brand_rows=int(args.brand_rows),
            brands_per_market=int(args.brands_per_market),
            large_market_limit=int(args.large_market_limit),
            token_env=str(args.token_env),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the replay CLI and write a structured summary."""
    options = parse_args(argv)
    result = replay(options)
    options.audit_dir.mkdir(parents=True, exist_ok=True)
    output = options.audit_dir / "brand_activity_replay_summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
