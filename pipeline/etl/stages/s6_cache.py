from __future__ import annotations

from typing import Any

from pipeline.etl.io.cache.archive_runner import run_archive_builders
from pipeline.etl.io.cache.db import copy_inputs, drop_old_cache_cause_tables, recreate_database, table_counts
from pipeline.etl.io.cache.schema import CACHE_TABLES, create_cache_tables

STAGE = "s6 cache"


def run(params: dict[str, Any]) -> int:
    target_db = params.get("target_db")
    source_db = params.get("source_db") or "jw_mart"
    strategic_source_db = params.get("strategic_source_db") or source_db
    event_source_db = params.get("event_source_db") or "jw_mart"
    smoke_market = params.get("ml_id") if params.get("dry_run") or params.get("ml_id") else None
    if not isinstance(target_db, str) or not target_db:
        print(f"[{STAGE}] target_db is required for isolated cache builds")
        return 2
    print(
        f"[{STAGE}] target_db={target_db} source_db={source_db} "
        f"strategic_source_db={strategic_source_db} smoke_market={smoke_market}"
    )
    recreate_database(
        target_db,
        protected_dbs=(str(source_db), str(strategic_source_db), str(event_source_db), "jw_mart"),
    )
    copied = copy_inputs(
        general_db=str(source_db),
        strategic_db=str(strategic_source_db),
        target_db=target_db,
        event_db=str(event_source_db),
    )
    print(f"[{STAGE}] copied_inputs={copied}")
    create_cache_tables(target_db)
    results = run_archive_builders(target_db, smoke_market=smoke_market)
    for result in results:
        print(f"[{STAGE}] builder={result.script} rc={result.rc}")
    failed = [result for result in results if result.rc != 0]
    if failed:
        return failed[0].rc or 1
    dropped = drop_old_cache_cause_tables(target_db)
    if dropped:
        print(f"[{STAGE}] dropped_archive_old_tables={dropped}")
    counts = table_counts(target_db, CACHE_TABLES)
    print(f"[{STAGE}] cache_counts={counts}")
    return 0
