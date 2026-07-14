from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pymysql

from pipeline.etl.io.mart.general_config import PROJECT_ROOT
from pipeline.scripts.deploy.mart_load_ops import PROTECTED_TARGETS, PublishAction, _publish_one, connect_admin
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
F124A_TARGET_DB: Final[str] = "jw_mart_d2_stage_20260630_r2"
F124A_LIVE_TABLE: Final[str] = "mart_general_filter_dimension_metric"
F124A_STAGING_TABLE: Final[str] = f"{F124A_LIVE_TABLE}__staging_f124a"


class ImagePullPreflightError(RuntimeError):
    """Raised when Kubernetes cannot pull the exact candidate digest."""


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


def probe_candidate_image_pullable(
    image: str,
    *,
    namespace: str,
    run_id: str,
    timeout_seconds: int = 90,
) -> None:
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
        raise ImagePullPreflightError("candidate image must use a full sha256 digest")
    safe_run_id = re.sub(r"[^a-z0-9-]", "-", run_id.lower()).strip("-")[-32:] or "probe"
    pod_name = f"f124a-image-{safe_run_id}"
    create = subprocess.run(
        [
            "kubectl",
            "run",
            pod_name,
            "--namespace",
            namespace,
            "--restart=Never",
            f"--image={image}",
            "--command",
            "--",
            "/bin/sh",
            "-c",
            "exit 0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise ImagePullPreflightError(create.stderr.strip() or "candidate image probe pod creation failed")
    try:
        wait = subprocess.run(
            [
                "kubectl",
                "wait",
                "--namespace",
                namespace,
                f"pod/{pod_name}",
                "--for=jsonpath={.status.phase}=Succeeded",
                f"--timeout={timeout_seconds}s",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if wait.returncode != 0:
            raise ImagePullPreflightError(wait.stderr.strip() or wait.stdout.strip() or "candidate image is not pullable")
    finally:
        subprocess.run(
            ["kubectl", "delete", "pod", pod_name, "--namespace", namespace, "--wait=false"],
            check=False,
            capture_output=True,
            text=True,
        )


def publish_f124a_general_dimension(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    run_id: str,
    lock_wait_timeout_seconds: int = 10,
) -> PublishAction:
    if target_db != F124A_TARGET_DB:
        raise ValueError(f"F-124a only permits target_db={F124A_TARGET_DB}")
    if not run_id or not run_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("run_id must contain only letters, numbers, underscore, and hyphen")
    if not table_exists(conn, target_db, F124A_LIVE_TABLE):
        raise RuntimeError(f"live table missing: {target_db}.{F124A_LIVE_TABLE}")
    if not table_exists(conn, target_db, F124A_STAGING_TABLE):
        raise RuntimeError(f"staging table missing: {target_db}.{F124A_STAGING_TABLE}")
    backup = f"{F124A_LIVE_TABLE}__old_{run_id}"
    if table_exists(conn, target_db, backup):
        raise RuntimeError(f"backup table already exists: {target_db}.{backup}")
    if lock_wait_timeout_seconds < 1:
        raise ValueError("lock_wait_timeout_seconds must be positive")

    transactions, metadata_locks = _f124a_lock_holders(
        conn,
        target_db=target_db,
        lock_wait_timeout_seconds=lock_wait_timeout_seconds,
    )
    if transactions:
        raise RuntimeError(f"active transaction may block F-124a publish: {transactions}")
    if metadata_locks:
        raise RuntimeError(f"metadata lock holder may block F-124a publish: {metadata_locks}")

    with conn.cursor() as cur:
        cur.execute(
            f"RENAME TABLE {quote_id(target_db)}.{quote_id(F124A_LIVE_TABLE)} "
            f"TO {quote_id(target_db)}.{quote_id(backup)}, "
            f"{quote_id(target_db)}.{quote_id(F124A_STAGING_TABLE)} "
            f"TO {quote_id(target_db)}.{quote_id(F124A_LIVE_TABLE)}"
        )
    digest = table_digest(conn, target_db, F124A_LIVE_TABLE)
    return PublishAction(F124A_LIVE_TABLE, "atomic_rename", F124A_LIVE_TABLE, backup, digest.row_count)


def _f124a_lock_holders(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    lock_wait_timeout_seconds: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    with conn.cursor() as cur:
        cur.execute("SET SESSION lock_wait_timeout=%s", (lock_wait_timeout_seconds,))
        cur.execute(
            """
            SELECT trx.trx_mysql_thread_id, trx.trx_started, trx.trx_state
            FROM information_schema.innodb_trx AS trx
            JOIN information_schema.processlist AS process
              ON process.id = trx.trx_mysql_thread_id
            WHERE trx.trx_mysql_thread_id <> CONNECTION_ID()
              AND process.db = %s
            ORDER BY trx.trx_started
            """,
            (target_db,),
        )
        transactions = list(cur.fetchall())
        cur.execute(
            """
            SELECT owner_thread_id, object_name, lock_type, lock_duration, lock_status
            FROM performance_schema.metadata_locks
            WHERE object_schema=%s AND object_name IN (%s, %s)
              AND owner_thread_id <> COALESCE(
                  (
                      SELECT thread_id
                      FROM performance_schema.threads
                      WHERE processlist_id = CONNECTION_ID()
                  ),
                  -1
              )
            ORDER BY owner_thread_id, object_name
            """,
            (target_db, F124A_LIVE_TABLE, F124A_STAGING_TABLE),
        )
        metadata_locks = list(cur.fetchall())
    return transactions, metadata_locks


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
    parser.add_argument("--build-db")
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--catalog-root", type=Path, default=PROJECT_ROOT / "output" / "catalog")
    parser.add_argument("--allow-operating-target", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--f124a-general-dimension", action="store_true")
    parser.add_argument("--candidate-image")
    parser.add_argument("--pull-probe-namespace", default="llmops")
    parser.add_argument("--lock-wait-timeout", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.f124a_general_dimension:
        try:
            if not args.candidate_image:
                raise ValueError("--candidate-image is required for F-124a publish")
            probe_candidate_image_pullable(
                str(args.candidate_image),
                namespace=str(args.pull_probe_namespace),
                run_id=str(args.run_id),
            )
            conn = connect_admin()
            try:
                action = publish_f124a_general_dimension(
                    conn,
                    target_db=str(args.target_db),
                    run_id=str(args.run_id),
                    lock_wait_timeout_seconds=int(args.lock_wait_timeout),
                )
            finally:
                conn.close()
        except (ValueError, RuntimeError, pymysql.MySQLError) as exc:
            print(f"F-124a publish refused: {exc}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "mode": "f124a_general_dimension",
                    "table": action.table,
                    "target_table": action.target_table,
                    "backup_table": action.backup_table,
                    "row_count": action.row_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not args.build_db:
        print("strategic reload publish requires --build-db", file=sys.stderr)
        return 1
    conn = connect_admin()
    try:
        summary = publish_strategic_reload_tables(
            conn,
            build_db=str(args.build_db),
            target_db=str(args.target_db),
            run_id=str(args.run_id),
            catalog_root=args.catalog_root,
            allow_operating_target=bool(args.allow_operating_target),
            dry_run=bool(args.dry_run),
        )
    finally:
        conn.close()
    print(json.dumps(_summary_payload(summary), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
