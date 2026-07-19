from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from typing import Any

from pipeline.scripts.deploy.analysis_cache_db import connect_admin, validate_schema_name
from pipeline.scripts.deploy.mart_load_verify import quote_id
from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.mysql_ops import MySQLMart
from pipeline.scripts.rollback.planner import build_retention_plan, build_rollback_plan
from pipeline.scripts.rollback.service import execute_rollback


def _rollback_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rollback one complete promoted mart generation.")
    parser.add_argument("--to", required=True, help="Promotion run id or latest-good")
    parser.add_argument("--target-db", default=os.environ.get("DB_NAME"))
    parser.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    parser.add_argument("--reason", default="operator requested rollback")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--yes", action="store_true")
    return parser


def _retention_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List or apply ledger-ordered mart retention.")
    parser.add_argument("--target-db", default=os.environ.get("DB_NAME"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--keep-generations", type=int, default=2)
    parser.add_argument("--keep-backup-runs", type=int, default=3)
    return parser


def parse_args(argv: list[str] | None = None) -> tuple[str, argparse.Namespace]:
    values = list(argv or [])
    if values and values[0] == "retention":
        return "retention", _retention_parser().parse_args(values[1:])
    return "rollback", _rollback_parser().parse_args(values)


def main(argv: list[str] | None = None) -> int:
    import sys

    action, args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.target_db:
        raise SystemExit("DB_NAME or --target-db is required")
    validate_schema_name("target_db", args.target_db)
    conn = connect_admin()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"USE {quote_id(args.target_db)}")
        ledger = PromotionLedger(conn, dialect="mysql")
        mart = MySQLMart(conn)
        if action == "rollback":
            payload = _run_rollback(ledger, mart, args)
        else:
            payload = _run_retention(ledger, mart, args)
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run_rollback(ledger: PromotionLedger, mart: MySQLMart, args: argparse.Namespace) -> dict[str, Any]:
    plan = build_rollback_plan(ledger, mart, target=args.to, serving_db=args.target_db)
    result = execute_rollback(
        ledger,
        mart,
        plan,
        actor=args.actor,
        reason=args.reason,
        dry_run=not args.yes,
        yes=args.yes,
    )
    return {"action": "rollback", "plan": asdict(plan), "result": asdict(result)}


def _run_retention(ledger: PromotionLedger, mart: MySQLMart, args: argparse.Namespace) -> dict[str, Any]:
    plan = build_retention_plan(
        ledger,
        serving_db=args.target_db,
        keep_generation_count=args.keep_generations,
        keep_backup_run_count=args.keep_backup_runs,
    )
    if args.apply:
        if not args.yes:
            raise RuntimeError("retention --apply requires --yes")
        for db_name in plan.generation_candidates:
            mart.drop_generation(db_name)
        for run_id in plan.backup_run_candidates:
            tables = tuple(
                table.backup_table
                for group in ledger.components(run_id).values()
                for table in group
            )
            mart.drop_backup_tables(args.target_db, tables)
    return {"action": "retention", "applied": bool(args.apply), "plan": asdict(plan)}


if __name__ == "__main__":
    raise SystemExit(main())
