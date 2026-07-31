from __future__ import annotations

import sqlite3

import pytest

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import TableBackup
from pipeline.scripts.rollback.planner import build_fdm_rollback_plan
from pipeline.scripts.rollback.service import (
    execute_fdm_rollback,
    recover_incomplete_fdm_rollback,
)


class InjectedCrash(BaseException):
    pass


class IsolatedMart:
    def __init__(self) -> None:
        self.tables: dict[str, tuple[int, str]] = {}
        self.rename_calls: list[tuple[tuple[str, str], ...]] = []
        self.invalidations = 0
        self.fail_invalidation = False

    def add(self, name: str, rows: int, digest: str) -> None:
        self.tables[name] = (rows, digest)

    def exists(self, _db_name: str, table_name: str) -> bool:
        return table_name in self.tables

    def count(self, _db_name: str, table_name: str) -> int:
        return self.tables[table_name][0]

    def digest(self, _db_name: str, table_name: str) -> str:
        return self.tables[table_name][1]

    def rename(self, _db_name: str, moves: tuple[tuple[str, str], ...]) -> None:
        before = dict(self.tables)
        sources = {source for source, _target in moves}
        for source, target in moves:
            if source not in before or (target in before and target not in sources):
                raise RuntimeError(f"invalid atomic rename {source} -> {target}")
        self.tables = {
            **{name: value for name, value in before.items() if name not in sources},
            **{target: before[source] for source, target in moves},
        }
        self.rename_calls.append(moves)

    def invalidate_dynamic_cache(self, _db_name: str) -> None:
        if self.fail_invalidation:
            raise RuntimeError("injected cache invalidation failure")
        self.invalidations += 1

    def rollback(self) -> None:
        return None


def _ledger() -> PromotionLedger:
    ledger = PromotionLedger(sqlite3.connect(":memory:"), dialect="sqlite")
    ledger.ensure_tables()
    return ledger


def _record_fdm_generation(
    ledger: PromotionLedger,
    mart: IsolatedMart,
    *,
    run_id: str = "fdm_run",
    backup_digest: str = "old-fdm",
) -> None:
    live = "mart_general_filter_dimension_metric"
    backup = f"{live}__old_{run_id}"
    mart.add(live, 4, "new-fdm")
    mart.add(backup, 3, backup_digest)
    ledger.record_component(
        promotion_run_id=run_id,
        component="fdm",
        epoch="2026-05",
        ingest_run_id="ingest-202605",
        target_db="serving",
        generation_db="fdm-stage",
        tables=(TableBackup(live, backup, 3, "old-fdm"),),
    )


def _plan(
    ledger: PromotionLedger,
    mart: IsolatedMart,
    *,
    run_id: str = "fdm_run",
    expected_rows: int = 3,
    expected_digest: str = "old-fdm",
):
    return build_fdm_rollback_plan(
        ledger,
        mart,
        target=run_id,
        serving_db="serving",
        expected_rows=expected_rows,
        expected_digest=expected_digest,
    )


def test_fdm_rollback_rejects_run_missing_from_promotion_ledger() -> None:
    ledger = _ledger()
    mart = IsolatedMart()

    with pytest.raises(RuntimeError, match="rollback generation not found"):
        _plan(ledger, mart, run_id="missing")


def test_fdm_rollback_rejects_operator_digest_drift() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)

    with pytest.raises(RuntimeError, match="requested backup digest mismatch"):
        _plan(ledger, mart, expected_digest="forged")


def test_fdm_rollback_rejects_operator_row_count_drift() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)

    with pytest.raises(RuntimeError, match="requested backup row count mismatch"):
        _plan(ledger, mart, expected_rows=4)


def test_fdm_rollback_rejects_four_component_generation() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)
    for component in ("general", "strategic", "analysis_cache"):
        live = f"{component}_live"
        backup = f"{live}__old_fdm_run"
        mart.add(live, 9, f"current-{component}")
        mart.add(backup, 8, f"backup-{component}")
        ledger.record_component(
            promotion_run_id="fdm_run",
            component=component,
            epoch="2026-05",
            ingest_run_id="ingest-202605",
            target_db="serving",
            generation_db="fdm-stage",
            tables=(
                TableBackup(live, backup, 8, f"backup-{component}"),
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="FDM-only rollback requires an exact scoped component set",
    ):
        _plan(ledger, mart)


def test_fdm_rollback_rejects_same_row_count_with_different_content() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart, backup_digest="tampered")

    with pytest.raises(RuntimeError, match="rollback backup digest mismatch"):
        _plan(ledger, mart)

    assert mart.rename_calls == []


def test_fdm_only_rollback_leaves_other_components_unchanged() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)
    for component in ("general", "strategic", "analysis_cache"):
        mart.add(f"{component}_live", 9, f"unchanged-{component}")
    before = dict(mart.tables)

    result = execute_fdm_rollback(
        ledger,
        mart,
        _plan(ledger, mart),
        actor="operator",
        reason="restore FDM only",
        yes=True,
    )

    assert result.changed is True
    for component in ("general", "strategic", "analysis_cache"):
        assert mart.tables[f"{component}_live"] == before[f"{component}_live"]
    assert mart.tables["mart_general_filter_dimension_metric"] == (3, "old-fdm")


def test_fdm_rollback_crash_is_compensated_on_restart() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)
    plan = _plan(ledger, mart)

    def crash_after_rename(point: str) -> None:
        if point == "after_rename":
            raise InjectedCrash("simulated process death")

    with pytest.raises(InjectedCrash):
        execute_fdm_rollback(
            ledger,
            mart,
            plan,
            actor="operator",
            reason="crash fixture",
            yes=True,
            crash_hook=crash_after_rename,
        )

    assert recover_incomplete_fdm_rollback(
        ledger,
        mart,
        promotion_run_id="fdm_run",
        target_db="serving",
        expected_rows=3,
        expected_digest="old-fdm",
    ) == "compensated"

    assert mart.tables["mart_general_filter_dimension_metric"] == (4, "new-fdm")
    assert mart.tables["mart_general_filter_dimension_metric__old_fdm_run"] == (
        3,
        "old-fdm",
    )
    assert ledger.fdm_rollback_state("fdm_run").state == "compensated"


def test_fdm_rollback_crash_after_ledger_event_finishes_on_restart() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)
    plan = _plan(ledger, mart)

    def crash_after_record(point: str) -> None:
        if point == "after_record":
            raise InjectedCrash("simulated post-ledger process death")

    with pytest.raises(InjectedCrash):
        execute_fdm_rollback(
            ledger,
            mart,
            plan,
            actor="operator",
            reason="post-ledger crash fixture",
            yes=True,
            crash_hook=crash_after_record,
        )

    assert recover_incomplete_fdm_rollback(
        ledger,
        mart,
        promotion_run_id="fdm_run",
        target_db="serving",
        expected_rows=3,
        expected_digest="old-fdm",
    ) == "completed"
    assert mart.tables["mart_general_filter_dimension_metric"] == (3, "old-fdm")
    assert ledger.fdm_rollback_state("fdm_run").state == "complete"


def test_fdm_rollback_failure_automatically_restores_safe_state() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_fdm_generation(ledger, mart)
    mart.fail_invalidation = True

    with pytest.raises(RuntimeError, match="previous live table restored"):
        execute_fdm_rollback(
            ledger,
            mart,
            _plan(ledger, mart),
            actor="operator",
            reason="failure fixture",
            yes=True,
        )

    assert mart.tables["mart_general_filter_dimension_metric"] == (4, "new-fdm")
    assert mart.tables["mart_general_filter_dimension_metric__old_fdm_run"] == (
        3,
        "old-fdm",
    )
    assert ledger.fdm_rollback_state("fdm_run").state == "compensated"
