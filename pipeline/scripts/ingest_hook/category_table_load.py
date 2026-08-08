"""Load one ingest category into explicitly isolated database tables."""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final

from pipeline.scripts.ingest_hook import config as ingest_config
from pipeline.scripts.ingest_hook.row_count_verifier import (
    LoadKind,
    RowCountEvidence,
    verify_row_counts,
)

ISOLATED_DB_PATTERN: Final = re.compile(
    r"^(?:jw_(?:ingest|mart_ingest)_[A-Za-z0-9_]+|"
    r"jw_brand_activity_keyword_[0-9]{20})$"
)


@dataclass(frozen=True, slots=True)
class LoadRequest:
    category: str
    sources: tuple[Path, ...]
    target_dir: Path
    epoch: str
    target_db: str


@dataclass(frozen=True, slots=True)
class LoadOutcome:
    loader: str
    primary: RowCountEvidence
    tables: tuple[RowCountEvidence, ...]


class TableLoaderUnavailableError(RuntimeError):
    pass


Loader = Callable[[LoadRequest], LoadOutcome]


def _isolated_target_db() -> str:
    target_db = os.environ.get(ingest_config.ENV_LOAD_STAGING_DB, "").strip()
    if not target_db:
        raise TableLoaderUnavailableError(
            f"{ingest_config.ENV_LOAD_STAGING_DB} is required for isolated table loading"
        )
    if ISOLATED_DB_PATTERN.fullmatch(target_db) is None:
        raise TableLoaderUnavailableError(
            f"refusing non-isolated target database {target_db!r}; "
            "expected jw_ingest_*, jw_mart_ingest_*, or a run-scoped "
            "jw_brand_activity_keyword_<20-digit-run-id> candidate"
        )
    return target_db


def _count_rows(database: str, table: str) -> int:
    from pipeline.etl.io.iqvia_loader import connect

    connection = connect(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
            return int(cursor.fetchone()[0])
    finally:
        connection.close()


def _count_rows_if_present(database: str, table: str) -> int:
    import pymysql

    try:
        return _count_rows(database, table)
    except pymysql.err.ProgrammingError as exc:
        if exc.args[0] in {1049, 1146}:
            return 0
        raise


def _difference_reason(source_rows: int, loaded_rows: int) -> tuple[str, ...]:
    if source_rows == loaded_rows:
        return ()
    return (f"duplicate_or_previously_loaded={source_rows - loaded_rows}",)


def _load_nsa(request: LoadRequest) -> LoadOutcome:
    from pipeline.etl.io import iqvia_loader

    before = _count_rows(request.target_db, iqvia_loader.NSA_TABLE)
    stats = iqvia_loader.load_source(
        list(request.sources),
        batch_size=10_000,
        target_database=request.target_db,
    )
    if stats.errors:
        raise RuntimeError("IQVIA NSA loader failed: " + "; ".join(stats.errors))
    after = _count_rows(request.target_db, iqvia_loader.NSA_TABLE)
    source_rows = stats.source_rows
    primary = verify_row_counts(
        RowCountEvidence(
            schema=request.target_db,
            table=iqvia_loader.NSA_TABLE,
            kind=LoadKind.APPEND,
            rows_before=before,
            rows_after=after,
            rows_loaded=stats.rows,
            source_rows=source_rows,
            difference_reasons=_difference_reason(source_rows, stats.rows),
        )
    )
    return LoadOutcome(loader="iqvia_loader", primary=primary, tables=(primary,))


def _load_brand_activity(request: LoadRequest, *, stage_scope: str) -> LoadOutcome:
    from pipeline.scripts.etl.brand_activity.ingest_keyword import read_keyword_events
    from pipeline.scripts.etl.brand_activity.km_core import source_sha256
    from pipeline.scripts.etl.brand_activity.raw_db import DbConfig, SourceRows, load_sources
    from pipeline.scripts.etl.brand_activity.raw_extract import read_csd_source_rows

    csd_rows = []
    keyword_rows = []
    if stage_scope == "csd":
        csd_rows = [
            row
            for source in request.sources
            for row in read_csd_source_rows(source, source_sha256(source))
        ]
    else:
        keyword_rows = [
            row for source in request.sources for row in read_keyword_events(source)
        ]
    raw_schema = f"{request.target_db}_raw"
    stage_schema = f"{request.target_db}_stage"
    config = DbConfig(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER", ""),
        password=os.environ.get("MARIADB_PASSWORD", ""),
        raw_schema=raw_schema,
        stage_schema=stage_schema,
    )
    stats = load_sources(
        config,
        SourceRows(csd=csd_rows, keyword=keyword_rows),
        window=None,
        stage_scope=stage_scope,
    )
    raw_table = "raw_csd_channel_dynamics" if stage_scope == "csd" else "raw_keyword_events"
    stage_table = "csd_channel_dynamics_stage" if stage_scope == "csd" else "km_keyword_event_stage"
    source_rows = len(csd_rows) if stage_scope == "csd" else len(keyword_rows)
    inserted = stats.inserted[raw_table]
    primary = verify_row_counts(
        RowCountEvidence(
            schema=raw_schema,
            table=raw_table,
            kind=LoadKind.APPEND,
            rows_before=stats.raw_before[raw_table],
            rows_after=stats.raw_after[raw_table],
            rows_loaded=inserted,
            source_rows=source_rows,
            difference_reasons=_difference_reason(source_rows, inserted),
        )
    )
    stage_after = stats.stage_rows[stage_table]
    stage_reasons = (
        ()
        if source_rows == stage_after
        else (f"stage_rebuilt_from_all_raw_rows source_rows={source_rows} stage_rows={stage_after}",)
    )
    stage = verify_row_counts(
        RowCountEvidence(
            schema=stage_schema,
            table=stage_table,
            kind=LoadKind.REPLACE,
            rows_before=stats.stage_before[stage_table],
            rows_after=stage_after,
            rows_loaded=stage_after,
            source_rows=source_rows,
            difference_reasons=stage_reasons,
        )
    )
    return LoadOutcome(
        loader=f"brand_activity_raw_db:{stage_scope}",
        primary=primary,
        tables=(primary, stage),
    )


def _load_csd(request: LoadRequest) -> LoadOutcome:
    return _load_brand_activity(request, stage_scope="csd")


def _load_keyword(request: LoadRequest) -> LoadOutcome:
    return _load_brand_activity(request, stage_scope="keyword")


def _load_mi_master(request: LoadRequest) -> LoadOutcome:
    from pipeline.scripts.etl.brand_activity import master_market_group_load

    if len(request.sources) != 1:
        raise TableLoaderUnavailableError(
            f"mi_master requires exactly one canonical workbook; got {len(request.sources)}"
        )
    tables = (
        master_market_group_load.MARKET_DEFINITION_TABLE,
        master_market_group_load.MAPPING_TABLE,
    )
    before = {table: _count_rows_if_present(request.target_db, table) for table in tables}
    summary = master_market_group_load.load(
        request.sources[0], schema=request.target_db, save=True
    )
    rebuilt = {
        master_market_group_load.MARKET_DEFINITION_TABLE: summary.market_definition,
        master_market_group_load.MAPPING_TABLE: summary.mapping,
    }
    evidence = tuple(
        verify_row_counts(
            RowCountEvidence(
                schema=request.target_db,
                table=table,
                kind=LoadKind.REPLACE,
                rows_before=before[table],
                rows_after=_count_rows(request.target_db, table),
                rows_loaded=rebuilt[table],
                source_rows=rebuilt[table],
                difference_reasons=(),
            )
        )
        for table in tables
    )
    return LoadOutcome(
        loader="master_market_group_load", primary=evidence[0], tables=evidence
    )


_LOADERS: dict[str, Loader] = {
    "iqvia_nsa": _load_nsa,
    "iqvia_csd_channel": _load_csd,
    "iqvia_csd_keyword": _load_keyword,
    "mi_master": _load_mi_master,
}


def load(
    category: str,
    sources: Path | tuple[Path, ...] | list[Path],
    target_dir: Path,
    epoch: str,
) -> dict[str, str | int]:
    """Run a category's canonical adapter and persist count-only evidence."""
    loader = _LOADERS.get(category)
    if loader is None:
        raise TableLoaderUnavailableError(f"no table loader for category {category!r}")
    normalized_sources = (sources,) if isinstance(sources, Path) else tuple(sources)
    request = LoadRequest(
        category=category,
        sources=normalized_sources,
        target_dir=target_dir,
        epoch=epoch,
        target_db=_isolated_target_db(),
    )
    outcome = loader(request)
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "ingest-table-load-v1",
        "category": category,
        "epoch": epoch,
        "loader": outcome.loader,
        "primary": outcome.primary.as_dict(),
        "tables": [item.as_dict() for item in outcome.tables],
    }
    (target_dir / "_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "rows_before": outcome.primary.rows_before,
        "rows_after": outcome.primary.rows_after,
        "rows_loaded": outcome.primary.rows_loaded,
        "manifest": str(target_dir / "_manifest.json"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True)
    parser.add_argument("--file", action="append", required=True, type=Path)
    parser.add_argument("--target-dir", required=True, type=Path)
    parser.add_argument("--epoch", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(load(args.category, args.file, args.target_dir, args.epoch), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
