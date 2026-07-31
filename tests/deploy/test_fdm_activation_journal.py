from __future__ import annotations

import sqlite3

import pytest

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import TableBackup
from pipeline.scripts.rollback.service import recover_incomplete_fdm_activation


class IsolatedActivationMart:
    def __init__(self) -> None:
        self.tables: dict[str, tuple[int, str]] = {}
        self.rename_calls: list[tuple[tuple[str, str], ...]] = []
        self.invalidations = 0
        self.dropped: list[str] = []

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

    def invalidate_dynamic_cache(
        self,
        _db_name: str,
        *,
        source: str | None = None,
    ) -> None:
        assert source in {None, "ubist"}
        self.invalidations += 1

    def drop_tables(self, _db_name: str, table_names: tuple[str, ...]) -> None:
        for table_name in table_names:
            self.tables.pop(table_name, None)
            self.dropped.append(table_name)

    def rollback(self) -> None:
        return None


def _ledger() -> PromotionLedger:
    ledger = PromotionLedger(sqlite3.connect(":memory:"), dialect="sqlite")
    ledger.ensure_tables()
    return ledger


def _record(
    ledger: PromotionLedger,
    event: str,
    *,
    run_id: str = "fdm_forward",
    batch_index: int | None = None,
    rows_affected: int = 0,
    pre_live_rows: int | None = None,
    pre_live_digest: str | None = None,
) -> None:
    ledger.record_fdm_activation_event(
        promotion_run_id=run_id,
        target_db="serving",
        live_table="mart_general_filter_dimension_metric",
        stage_table=f"mart_general_filter_dimension_metric__stage_{run_id}",
        backup_table=f"mart_general_filter_dimension_metric__old_{run_id}",
        source="ubist",
        event=event,
        batch_index=batch_index,
        rows_affected=rows_affected,
        pre_live_rows=pre_live_rows,
        pre_live_digest=pre_live_digest,
    )


def test_activation_journal_preserves_each_completed_batch() -> None:
    ledger = _ledger()
    _record(ledger, "started")
    _record(ledger, "candidate_copy_batch", batch_index=1, rows_affected=200)
    _record(ledger, "candidate_copy_batch", batch_index=2, rows_affected=37)
    _record(
        ledger,
        "candidate_ready",
        pre_live_rows=237,
        pre_live_digest="237:10:20",
    )

    events = ledger.fdm_activation_events("fdm_forward")

    assert [event.event for event in events] == [
        "started",
        "candidate_copy_batch",
        "candidate_copy_batch",
        "candidate_ready",
    ]
    assert [event.batch_index for event in events[1:3]] == [1, 2]
    assert [event.rows_affected for event in events[1:3]] == [200, 37]


def test_restart_compensates_crash_after_atomic_activation() -> None:
    ledger = _ledger()
    mart = IsolatedActivationMart()
    live = "mart_general_filter_dimension_metric"
    stage = f"{live}__stage_fdm_forward"
    backup = f"{live}__old_fdm_forward"
    mart.tables[live] = (238, "new")
    mart.tables[backup] = (237, "old")
    _record(ledger, "started")
    _record(
        ledger,
        "candidate_ready",
        pre_live_rows=237,
        pre_live_digest="old",
    )
    _record(ledger, "activation_prepared")

    result = recover_incomplete_fdm_activation(
        ledger,
        mart,
        promotion_run_id="fdm_forward",
        target_db="serving",
    )

    assert result == "compensated"
    assert mart.tables[live] == (237, "old")
    assert backup not in mart.tables
    assert stage not in mart.tables
    assert mart.invalidations == 1
    assert ledger.fdm_activation_events("fdm_forward")[-1].event == "compensated"


def test_restart_discards_hidden_candidate_without_touching_live() -> None:
    ledger = _ledger()
    mart = IsolatedActivationMart()
    live = "mart_general_filter_dimension_metric"
    stage = f"{live}__stage_fdm_forward"
    mart.tables[live] = (237, "old")
    mart.tables[stage] = (118, "partial")
    _record(ledger, "started")
    _record(ledger, "candidate_copy_batch", batch_index=1, rows_affected=118)

    result = recover_incomplete_fdm_activation(
        ledger,
        mart,
        promotion_run_id="fdm_forward",
        target_db="serving",
    )

    assert result == "compensated"
    assert mart.tables[live] == (237, "old")
    assert stage not in mart.tables
    assert mart.rename_calls == []
    assert mart.invalidations == 0


def test_completed_activation_is_not_compensated() -> None:
    ledger = _ledger()
    mart = IsolatedActivationMart()
    _record(ledger, "started")
    _record(ledger, "completed")

    assert (
        recover_incomplete_fdm_activation(
            ledger,
            mart,
            promotion_run_id="fdm_forward",
            target_db="serving",
        )
        is None
    )


def test_component_recorded_crash_completes_forward_without_compensation() -> None:
    ledger = _ledger()
    mart = IsolatedActivationMart()
    live = "mart_general_filter_dimension_metric"
    backup = f"{live}__old_fdm_forward"
    mart.tables[live] = (238, "new")
    mart.tables[backup] = (237, "old")
    _record(
        ledger,
        "candidate_ready",
        pre_live_rows=237,
        pre_live_digest="old",
    )
    _record(ledger, "cache_invalidated")
    ledger.record_component(
        promotion_run_id="fdm_forward",
        component="fdm",
        epoch="epoch",
        ingest_run_id="ingest",
        target_db="serving",
        generation_db="generation",
        tables=(
            TableBackup(
                live_table=live,
                backup_table=backup,
                expected_rows=237,
                expected_digest="old",
            ),
        ),
    )

    result = recover_incomplete_fdm_activation(
        ledger,
        mart,
        promotion_run_id="fdm_forward",
        target_db="serving",
    )

    assert result == "completed"
    assert mart.tables[live] == (238, "new")
    assert mart.tables[backup] == (237, "old")
    assert mart.rename_calls == []
    assert mart.invalidations == 1
    assert ledger.fdm_activation_events("fdm_forward")[-1].event == "completed"


def test_live_only_recovery_requires_pre_live_identity_match() -> None:
    ledger = _ledger()
    mart = IsolatedActivationMart()
    live = "mart_general_filter_dimension_metric"
    mart.tables[live] = (238, "unexpected")
    _record(
        ledger,
        "started",
        pre_live_rows=237,
        pre_live_digest="old",
    )

    with pytest.raises(RuntimeError, match="fail-closed"):
        recover_incomplete_fdm_activation(
            ledger,
            mart,
            promotion_run_id="fdm_forward",
            target_db="serving",
        )

    assert ledger.fdm_activation_events("fdm_forward")[-1].event == "recovery_failed"


def test_ambiguous_restart_state_fails_closed_and_records_failure() -> None:
    ledger = _ledger()
    mart = IsolatedActivationMart()
    live = "mart_general_filter_dimension_metric"
    stage = f"{live}__stage_fdm_forward"
    backup = f"{live}__old_fdm_forward"
    mart.tables[live] = (238, "new")
    mart.tables[stage] = (237, "candidate")
    mart.tables[backup] = (237, "old")
    _record(
        ledger,
        "candidate_ready",
        pre_live_rows=237,
        pre_live_digest="old",
    )

    with pytest.raises(RuntimeError, match="fail-closed"):
        recover_incomplete_fdm_activation(
            ledger,
            mart,
            promotion_run_id="fdm_forward",
            target_db="serving",
        )

    assert ledger.fdm_activation_events("fdm_forward")[-1].event == "recovery_failed"
