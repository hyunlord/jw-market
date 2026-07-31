from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import (
    FdmRollbackPlan,
    FdmRollbackState,
    RollbackPlan,
    RollbackResult,
)


class MartRollbackExecutor(Protocol):
    def exists(self, db_name: str, table_name: str) -> bool: ...

    def rename(self, db_name: str, moves: tuple[tuple[str, str], ...]) -> None: ...

    def invalidate_dynamic_cache(self, db_name: str) -> None: ...

    def count(self, db_name: str, table_name: str) -> int: ...

    def digest(self, db_name: str, table_name: str) -> str: ...

    def rollback(self) -> None: ...


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


def execute_fdm_rollback(
    ledger: PromotionLedger,
    executor: MartRollbackExecutor,
    plan: FdmRollbackPlan,
    *,
    actor: str,
    reason: str,
    dry_run: bool = True,
    yes: bool = False,
    crash_hook: Callable[[str], None] | None = None,
) -> RollbackResult:
    if dry_run and not yes:
        return RollbackResult(plan.promotion_run_id, False, 0)
    if not yes:
        raise RuntimeError("rollback mutation requires --yes")
    if not actor.strip() or not reason.strip():
        raise ValueError("rollback actor and reason are required")

    recovery = recover_incomplete_fdm_rollback(
        ledger,
        executor,
        promotion_run_id=plan.promotion_run_id,
        target_db=plan.target_db,
        expected_rows=plan.table.expected_rows,
        expected_digest=plan.table.expected_digest,
    )
    if recovery == "completed":
        return RollbackResult(plan.promotion_run_id, True, 1)
    if recovery == "compensated":
        raise RuntimeError(
            f"incomplete FDM rollback {plan.promotion_run_id} was automatically compensated"
        )

    ledger.prepare_fdm_rollback(plan, actor=actor, reason=reason)
    try:
        executor.rename(plan.target_db, plan.moves)
        if crash_hook is not None:
            crash_hook("after_rename")
        ledger.update_fdm_rollback_state(plan.promotion_run_id, "swapped")
        executor.invalidate_dynamic_cache(plan.target_db)
        _validate_restored_fdm(executor, plan)
        ledger.update_fdm_rollback_state(plan.promotion_run_id, "verified")
        if crash_hook is not None:
            crash_hook("after_verify")
    except Exception as exc:
        _fail_with_compensation(ledger, executor, plan, exc)

    try:
        ledger.record_rollback(plan.promotion_run_id, actor=actor, reason=reason)
    except Exception as exc:
        _fail_with_compensation(ledger, executor, plan, exc)
    if crash_hook is not None:
        crash_hook("after_record")
    ledger.update_fdm_rollback_state(plan.promotion_run_id, "complete")
    return RollbackResult(plan.promotion_run_id, True, 1)


def recover_incomplete_fdm_rollback(
    ledger: PromotionLedger,
    executor: MartRollbackExecutor,
    *,
    promotion_run_id: str,
    target_db: str,
    expected_rows: int,
    expected_digest: str,
) -> str | None:
    state = ledger.fdm_rollback_state(promotion_run_id)
    if state is None or state.state not in {"prepared", "swapped", "verified"}:
        return None
    if (
        state.target_db != target_db
        or state.expected_rows != expected_rows
        or state.expected_digest != expected_digest
    ):
        raise RuntimeError(
            f"FDM rollback recovery identity mismatch: {promotion_run_id}"
        )
    if ledger.has_rollback_event(state.promotion_run_id):
        _validate_table(
            executor,
            state.target_db,
            state.live_table,
            state.expected_rows,
            state.expected_digest,
            label="completed FDM rollback",
        )
        ledger.update_fdm_rollback_state(state.promotion_run_id, "complete")
        return "completed"

    live_exists = executor.exists(state.target_db, state.live_table)
    backup_exists = executor.exists(state.target_db, state.backup_table)
    failed_exists = executor.exists(state.target_db, state.failed_table)
    if live_exists and not backup_exists and failed_exists:
        executor.rename(
            state.target_db,
            (
                (state.live_table, state.backup_table),
                (state.failed_table, state.live_table),
            ),
        )
    elif not (live_exists and backup_exists and not failed_exists):
        ledger.update_fdm_rollback_state(
            state.promotion_run_id,
            "recovery_failed",
            error=(
                "ambiguous physical state "
                f"live={live_exists} backup={backup_exists} failed={failed_exists}"
            ),
        )
        raise RuntimeError(
            "incomplete FDM rollback has ambiguous physical table state; fail-closed"
        )
    _validate_table(
        executor,
        state.target_db,
        state.live_table,
        state.pre_live_rows,
        state.pre_live_digest,
        label="compensated live",
    )
    _validate_table(
        executor,
        state.target_db,
        state.backup_table,
        state.expected_rows,
        state.expected_digest,
        label="compensated backup",
    )
    ledger.update_fdm_rollback_state(state.promotion_run_id, "compensated")
    return "compensated"


def _fail_with_compensation(
    ledger: PromotionLedger,
    executor: MartRollbackExecutor,
    plan: FdmRollbackPlan,
    exc: Exception,
) -> None:
    try:
        executor.rollback()
        _compensate_fdm_rollback(executor, plan)
        ledger.update_fdm_rollback_state(
            plan.promotion_run_id,
            "compensated",
            error=repr(exc),
        )
    except Exception as compensation_exc:
        ledger.update_fdm_rollback_state(
            plan.promotion_run_id,
            "recovery_failed",
            error=f"rollback={exc!r}; compensation={compensation_exc!r}",
        )
        raise RuntimeError(
            "FDM rollback failed and automatic restoration also failed: "
            f"rollback={exc!r}; restoration={compensation_exc!r}"
        ) from compensation_exc
    raise RuntimeError("FDM rollback failed; previous live table restored") from exc


def _compensate_fdm_rollback(
    executor: MartRollbackExecutor,
    plan: FdmRollbackPlan,
) -> None:
    if executor.exists(plan.target_db, plan.failed_table):
        executor.rename(plan.target_db, plan.compensation_moves)
    _validate_table(
        executor,
        plan.target_db,
        plan.table.live_table,
        plan.pre_live_rows,
        plan.pre_live_digest,
        label="compensated live",
    )
    _validate_table(
        executor,
        plan.target_db,
        plan.table.backup_table,
        plan.table.expected_rows,
        plan.table.expected_digest,
        label="compensated backup",
    )


def _validate_restored_fdm(
    executor: MartRollbackExecutor,
    plan: FdmRollbackPlan,
) -> None:
    _validate_table(
        executor,
        plan.target_db,
        plan.table.live_table,
        plan.table.expected_rows,
        plan.table.expected_digest,
        label="post-rollback FDM",
    )


def _validate_table(
    executor: MartRollbackExecutor,
    db_name: str,
    table_name: str,
    expected_rows: int,
    expected_digest: str,
    *,
    label: str,
) -> None:
    rows = executor.count(db_name, table_name)
    if rows != expected_rows:
        raise RuntimeError(
            f"{label} row count mismatch: {table_name} {rows} != {expected_rows}"
        )
    digest = executor.digest(db_name, table_name)
    if digest != expected_digest:
        raise RuntimeError(f"{label} digest mismatch: {table_name}")
