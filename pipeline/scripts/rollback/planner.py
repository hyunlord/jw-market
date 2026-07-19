from __future__ import annotations

from typing import Protocol

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import (
    REQUIRED_COMPONENTS,
    RetentionPlan,
    RollbackPlan,
    TableBackup,
)


DYNAMIC_CACHE_TABLE = "cache_dynamic_market_response"


class MartInspector(Protocol):
    def exists(self, db_name: str, table_name: str) -> bool: ...

    def count(self, db_name: str, table_name: str) -> int: ...

    def digest(self, db_name: str, table_name: str) -> str: ...


def build_rollback_plan(
    ledger: PromotionLedger,
    inspector: MartInspector,
    *,
    target: str,
    serving_db: str,
) -> RollbackPlan:
    generation = ledger.generation(target)
    if generation is None:
        raise RuntimeError(f"rollback generation not found: {target}")
    if generation.serving_db != serving_db:
        raise RuntimeError(
            f"rollback generation targets {generation.serving_db}, not runtime serving DB {serving_db}"
        )
    components = ledger.components(generation.promotion_run_id)
    missing = sorted(REQUIRED_COMPONENTS - components.keys())
    unknown = sorted(components.keys() - REQUIRED_COMPONENTS)
    if missing or unknown:
        raise RuntimeError(f"incomplete promotion set: missing={missing} unknown={unknown}")

    tables = _deduplicate_tables(tuple(table for group in components.values() for table in group))
    if not inspector.exists(serving_db, DYNAMIC_CACHE_TABLE):
        raise RuntimeError(f"dynamic cache invalidation target missing: {serving_db}.{DYNAMIC_CACHE_TABLE}")
    moves: list[tuple[str, str]] = []
    for table in tables:
        _validate_backup(inspector, serving_db, table)
        failed_table = _failed_table_name(table.live_table, generation.promotion_run_id)
        if inspector.exists(serving_db, failed_table):
            raise RuntimeError(f"rollback scratch table already exists: {serving_db}.{failed_table}")
        moves.extend(((table.live_table, failed_table), (table.backup_table, table.live_table)))
    return RollbackPlan(
        promotion_run_id=generation.promotion_run_id,
        target_db=serving_db,
        epoch=generation.epoch,
        ingest_run_id=generation.ingest_run_id,
        tables=tables,
        moves=tuple(moves),
        cache_tables=(DYNAMIC_CACHE_TABLE,),
        warning="Rollback reverses every source ingested after this generation; partial source rollback is unsupported.",
    )


def build_retention_plan(
    ledger: PromotionLedger,
    *,
    serving_db: str,
    keep_generation_count: int = 2,
    keep_backup_run_count: int = 3,
) -> RetentionPlan:
    if keep_generation_count < 2 or keep_backup_run_count < 1:
        raise ValueError("retention must keep at least two generations and one backup run")
    generations = ledger.generations()
    ordered_dbs = tuple(dict.fromkeys(row.generation_db for row in generations))
    retained = set(ordered_dbs[:keep_generation_count]) | {serving_db}
    generation_candidates = tuple(db_name for db_name in ordered_dbs if db_name not in retained)
    ordered_runs = tuple(row.promotion_run_id for row in generations)
    return RetentionPlan(
        protected_serving_db=serving_db,
        retained_generations=tuple(db_name for db_name in ordered_dbs if db_name in retained),
        generation_candidates=generation_candidates,
        retained_backup_runs=ordered_runs[:keep_backup_run_count],
        backup_run_candidates=ordered_runs[keep_backup_run_count:],
    )


def _deduplicate_tables(tables: tuple[TableBackup, ...]) -> tuple[TableBackup, ...]:
    by_live: dict[str, TableBackup] = {}
    for table in tables:
        previous = by_live.get(table.live_table)
        if previous is not None and previous != table:
            raise RuntimeError(f"conflicting backup metadata for live table: {table.live_table}")
        by_live[table.live_table] = table
    if not by_live:
        raise RuntimeError("promotion set contains no rollback tables")
    return tuple(by_live[name] for name in sorted(by_live))


def _validate_backup(inspector: MartInspector, db_name: str, table: TableBackup) -> None:
    if not inspector.exists(db_name, table.live_table):
        raise RuntimeError(f"live table missing: {db_name}.{table.live_table}")
    if not inspector.exists(db_name, table.backup_table):
        raise RuntimeError(f"rollback backup missing: {db_name}.{table.backup_table}")
    rows = inspector.count(db_name, table.backup_table)
    if rows < 1:
        raise RuntimeError(f"empty rollback backup: {db_name}.{table.backup_table}")
    if rows != table.expected_rows:
        raise RuntimeError(
            f"rollback backup row count mismatch: {db_name}.{table.backup_table} {rows} != {table.expected_rows}"
        )
    digest = inspector.digest(db_name, table.backup_table)
    if digest != table.expected_digest:
        raise RuntimeError(f"rollback backup digest mismatch: {db_name}.{table.backup_table}")


def _failed_table_name(live_table: str, run_id: str) -> str:
    name = f"{live_table}__failed_{run_id}"
    if len(name) > 64:
        raise ValueError(f"rollback scratch identifier exceeds 64 characters: {name}")
    return name
