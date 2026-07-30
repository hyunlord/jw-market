from __future__ import annotations

"""Restore one FDM promotion backup without the generic generation planner."""

import argparse
import json
from typing import Any

from pipeline.etl.io.mart.filter_dimension_promote import (
    rollback_filter_dimension_promotion,
)
from pipeline.scripts.etl.build_filter_dimension_metric import _connect_admin


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--promotion-run-id", required=True)
    parser.add_argument("--expected-backup-rows", required=True, type=int)
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
        "mode": "fdm_only_atomic_swap",
    }
    if not args.yes:
        return {**plan, "changed": False}
    conn = _connect_admin()
    try:
        result = rollback_filter_dimension_promotion(
            conn,
            target_db=args.target_db,
            promotion_run_id=args.promotion_run_id,
            expected_backup_rows=args.expected_backup_rows,
        )
        return {**plan, **result, "changed": True}
    finally:
        conn.close()


def main() -> None:
    print(json.dumps(run(parse_args()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
