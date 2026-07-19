from __future__ import annotations

from typing import Protocol

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import RollbackPlan, RollbackResult


class MartRollbackExecutor(Protocol):
    def rename(self, db_name: str, moves: tuple[tuple[str, str], ...]) -> None: ...

    def invalidate_dynamic_cache(self, db_name: str) -> None: ...

    def count(self, db_name: str, table_name: str) -> int: ...

    def digest(self, db_name: str, table_name: str) -> str: ...


def execute_rollback(
    ledger: PromotionLedger,
    executor: MartRollbackExecutor,
    plan: RollbackPlan,
    *,
    actor: str,
    reason: str,
    dry_run: bool = True,
    yes: bool = False,
) -> RollbackResult:
    if dry_run and not yes:
        return RollbackResult(plan.promotion_run_id, False, 0)
    if not yes:
        raise RuntimeError("rollback mutation requires --yes")
    if not actor.strip() or not reason.strip():
        raise ValueError("rollback actor and reason are required")

    executor.rename(plan.target_db, plan.moves)
    executor.invalidate_dynamic_cache(plan.target_db)
    for table in plan.tables:
        rows = executor.count(plan.target_db, table.live_table)
        if rows != table.expected_rows:
            raise RuntimeError(
                f"post-rollback row count mismatch: {table.live_table} {rows} != {table.expected_rows}"
            )
        if executor.digest(plan.target_db, table.live_table) != table.expected_digest:
            raise RuntimeError(f"post-rollback digest mismatch: {table.live_table}")
    ledger.record_rollback(plan.promotion_run_id, actor=actor, reason=reason)
    return RollbackResult(plan.promotion_run_id, True, len(plan.tables))
