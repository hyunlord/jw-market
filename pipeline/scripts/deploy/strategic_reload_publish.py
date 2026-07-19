from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pymysql

from pipeline.etl.io.mart.general_config import PROJECT_ROOT
from pipeline.scripts.deploy.mart_load_ops import PROTECTED_TARGETS, PublishAction, _publish_one, connect_admin
from pipeline.scripts.rollback.recording import (
    add_promotion_identity_args,
    identity_from_args,
    record_mysql_component,
)
from pipeline.scripts.deploy.mart_load_ops import validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_digest, table_exists

STRATEGIC_RELOAD_TABLES: Final[tuple[str, ...]] = (
    "mart_strategic_ml_brand_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_market_metric",
    "cache_brands",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
)
GENERAL_TABLE_PREFIX: Final[str] = "mart_general_"


@dataclass(frozen=True, slots=True)
class PublishSummary:
    run_id: str
    build_db: str
    target_db: str
    catalog_root: Path
    actions: tuple[PublishAction, ...]
    dry_run: bool
    rolled_back: bool
    elapsed_seconds: float


class PublishFailedError(RuntimeError):
    """Raised after a failed publish has attempted to restore prior tables."""


def validate_publish_tables(tables: tuple[str, ...]) -> tuple[str, ...]:
    if not tables:
        raise ValueError("at least one table is required")
    unknown = sorted(set(tables) - set(STRATEGIC_RELOAD_TABLES))
    general = [table for table in tables if table.startswith(GENERAL_TABLE_PREFIX)]
    if unknown or general:
        rejected = sorted(set(unknown + general))
        raise ValueError(f"unsupported strategic reload publish tables: {rejected}")
    return tables


def guard_publish_run(*, build_db: str, target_db: str, allow_operating_target: bool) -> None:
    validate_schema_name("build_db", build_db)
    validate_schema_name("target_db", target_db)
    if build_db == target_db:
        raise ValueError("build_db and target_db must differ")
    if build_db in PROTECTED_TARGETS:
        raise ValueError(f"refusing protected build_db: {build_db}")
    if target_db in PROTECTED_TARGETS and not allow_operating_target:
        raise RuntimeError("refusing operating target publish without --allow-operating-target")


def resolve_publish_catalog_root(catalog_root: Path | None) -> Path:
    expected = (PROJECT_ROOT / "output" / "catalog").resolve()
    selected = (catalog_root or expected).resolve()
    if selected != expected:
        raise ValueError(
            "strategic reload publish requires output/catalog because cache builders "
            f"read that root; received {selected}"
        )
    if not selected.exists():
        raise FileNotFoundError(f"catalog root not found: {selected}")
    return selected


def publish_strategic_reload_tables(
    conn: pymysql.connections.Connection,
    *,
    build_db: str,
    target_db: str,
    run_id: str,
    tables: tuple[str, ...] = STRATEGIC_RELOAD_TABLES,
    catalog_root: Path | None = None,
    allow_operating_target: bool = False,
    dry_run: bool = False,
    rollback_on_error: bool = True,
) -> PublishSummary:
    started = time.perf_counter()
    selected_tables = validate_publish_tables(tables)
    resolved_catalog_root = resolve_publish_catalog_root(catalog_root)
    guard_publish_run(build_db=build_db, target_db=target_db, allow_operating_target=allow_operating_target)
    if dry_run:
        actions = tuple(_dry_run_action(conn, build_db, table) for table in selected_tables)
        return PublishSummary(
            run_id=run_id,
            build_db=build_db,
            target_db=target_db,
            catalog_root=resolved_catalog_root,
            actions=actions,
            dry_run=True,
            rolled_back=False,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )

    actions: list[PublishAction] = []
    try:
        for table in selected_tables:
            actions.append(_publish_one(conn, build_db, target_db, table, run_id))
    except (RuntimeError, pymysql.MySQLError) as exc:
        if rollback_on_error:
            _restore_published_tables(conn, target_db, tuple(reversed(actions)), run_id)
        raise PublishFailedError(f"strategic reload publish failed for run_id={run_id}") from exc

    return PublishSummary(
        run_id=run_id,
        build_db=build_db,
        target_db=target_db,
        catalog_root=resolved_catalog_root,
        actions=tuple(actions),
        dry_run=False,
        rolled_back=False,
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )


def restore_published_table(
    conn: pymysql.connections.Connection,
    target_db: str,
    action: PublishAction,
    run_id: str,
) -> None:
    if not action.backup_table:
        return
    failed_table = f"{action.table}__failed_{run_id}"
    if table_exists(conn, target_db, failed_table):
        raise RuntimeError(f"rollback scratch table already exists: {target_db}.{failed_table}")
    if not table_exists(conn, target_db, action.backup_table):
        raise RuntimeError(f"rollback backup table missing: {target_db}.{action.backup_table}")
    with conn.cursor() as cur:
        cur.execute(
            f"RENAME TABLE {quote_id(target_db)}.{quote_id(action.table)} TO {quote_id(target_db)}.{quote_id(failed_table)}, "
            f"{quote_id(target_db)}.{quote_id(action.backup_table)} TO {quote_id(target_db)}.{quote_id(action.table)}"
        )


def _restore_published_tables(
    conn: pymysql.connections.Connection,
    target_db: str,
    actions: tuple[PublishAction, ...],
    run_id: str,
) -> None:
    for action in actions:
        restore_published_table(conn, target_db, action, run_id)


def _dry_run_action(conn: pymysql.connections.Connection, build_db: str, table: str) -> PublishAction:
    digest = table_digest(conn, build_db, table)
    return PublishAction(table, "dry_run", table, None, digest.row_count)


def _summary_payload(summary: PublishSummary) -> dict[str, object]:
    return {
        "run_id": summary.run_id,
        "build_db": summary.build_db,
        "target_db": summary.target_db,
        "catalog_root": str(summary.catalog_root),
        "dry_run": summary.dry_run,
        "rolled_back": summary.rolled_back,
        "elapsed_seconds": summary.elapsed_seconds,
        "tables": [
            {
                "table": action.table,
                "mode": action.mode,
                "target_table": action.target_table,
                "backup_table": action.backup_table,
                "row_count": action.row_count,
            }
            for action in summary.actions
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish the strategic reload 8-table set from an isolated build schema.")
    parser.add_argument("--build-db", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--catalog-root", type=Path, default=PROJECT_ROOT / "output" / "catalog")
    parser.add_argument("--allow-operating-target", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    add_promotion_identity_args(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = connect_admin()
    try:
        identity = None
        if not args.dry_run:
            identity = identity_from_args(
                args,
                promotion_run_id=str(args.run_id),
                serving_db=str(args.target_db),
                required=True,
            )
        summary = publish_strategic_reload_tables(
            conn,
            build_db=str(args.build_db),
            target_db=str(args.target_db),
            run_id=str(args.run_id),
            catalog_root=args.catalog_root,
            allow_operating_target=bool(args.allow_operating_target),
            dry_run=bool(args.dry_run),
        )
        if identity is not None:
            record_mysql_component(
                conn,
                identity=identity,
                component="strategic",
                table_pairs=tuple(
                    (action.table, action.backup_table)
                    for action in summary.actions
                    if action.backup_table is not None
                ),
            )
    finally:
        conn.close()
    print(json.dumps(_summary_payload(summary), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
