from __future__ import annotations

"""Restore one FDM promotion backup without the generic generation planner."""

import argparse
import json
from typing import Any

from pipeline.scripts.etl.build_filter_dimension_metric import _connect_admin
from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.mysql_ops import MySQLMart
from pipeline.scripts.rollback.planner import build_fdm_rollback_plan
from pipeline.scripts.rollback.service import (
    execute_fdm_rollback,
    recover_incomplete_fdm_rollback,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--ledger-db", required=True)
    parser.add_argument("--promotion-run-id", required=True)
    parser.add_argument("--expected-backup-rows", required=True, type=int)
    parser.add_argument("--expected-backup-digest", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Execute the atomic FDM-only rollback. Without this flag, print the plan.",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = {
        "target_db": args.target_db,
        "promotion_run_id": args.promotion_run_id,
        "expected_backup_rows": args.expected_backup_rows,
        "expected_backup_digest": args.expected_backup_digest,
        "mode": "fdm_only_ledger_verified_atomic_swap",
    }
    conn = _connect_admin()
    try:
        ledger = PromotionLedger(conn, dialect="mysql", schema_db=args.ledger_db)
        ledger.ensure_tables()
        mart = MySQLMart(conn)
        recovery = recover_incomplete_fdm_rollback(
            ledger,
            mart,
            promotion_run_id=args.promotion_run_id,
            target_db=args.target_db,
            expected_rows=args.expected_backup_rows,
            expected_digest=args.expected_backup_digest,
        )
        if recovery == "completed":
            return {**plan, "changed": True, "recovered": "completed"}
        if recovery == "compensated":
            raise RuntimeError(
                f"incomplete FDM rollback {args.promotion_run_id} was "
                "automatically compensated; submit a new explicit rollback request"
            )
        rollback_plan = build_fdm_rollback_plan(
            ledger,
            mart,
            target=args.promotion_run_id,
            serving_db=args.target_db,
            expected_rows=args.expected_backup_rows,
            expected_digest=args.expected_backup_digest,
        )
        result = execute_fdm_rollback(
            ledger,
            mart,
            rollback_plan,
            actor=args.actor,
            reason=args.reason,
            yes=args.yes,
        )
        return {
            **plan,
            "epoch": rollback_plan.epoch,
            "ingest_run_id": rollback_plan.ingest_run_id,
            "live_table": rollback_plan.table.live_table,
            "backup_table": rollback_plan.table.backup_table,
            "changed": result.changed,
        }
    finally:
        conn.close()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
