from __future__ import annotations

import sqlite3

import pytest

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import REQUIRED_COMPONENTS, TableBackup
from pipeline.scripts.rollback.planner import build_retention_plan, build_rollback_plan
from pipeline.scripts.rollback.recording import PromotionIdentity, record_component_backups
from pipeline.scripts.rollback.service import execute_rollback


class IsolatedMart:
    def __init__(self) -> None:
        self.tables: dict[str, tuple[int, str]] = {}
        self.rename_calls: list[tuple[tuple[str, str], ...]] = []
        self.invalidations = 0

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
        self.invalidations += 1


def _ledger() -> PromotionLedger:
    ledger = PromotionLedger(sqlite3.connect(":memory:"), dialect="sqlite")
    ledger.ensure_tables()
    return ledger


def _record_complete_generation(
    ledger: PromotionLedger,
    mart: IsolatedMart,
    *,
    run_id: str = "run2",
    target_db: str = "serving_stage_named_db",
) -> None:
    mart.add("cache_dynamic_market_response", 2, "cache")
    for index, component in enumerate(sorted(REQUIRED_COMPONENTS)):
        live = f"{component}_live"
        backup = f"{live}__old_{run_id}"
        mart.add(live, index + 10, f"new-{component}")
        mart.add(backup, index + 1, f"old-{component}")
        ledger.record_component(
            promotion_run_id=run_id,
            component=component,
            epoch="2026-03",
            ingest_run_id="job-202603",
            target_db=target_db,
            generation_db="generation-2",
            tables=(TableBackup(live, backup, index + 1, f"old-{component}"),),
        )


def test_complete_generation_round_trip_restores_exact_previous_values() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_complete_generation(ledger, mart)

    plan = build_rollback_plan(
        ledger,
        mart,
        target="run2",
        serving_db="serving_stage_named_db",
    )
    result = execute_rollback(
        ledger,
        mart,
        plan,
        actor="test-operator",
        reason="isolated round trip",
        yes=True,
    )

    assert result.changed is True
    assert len(mart.rename_calls) == 1
    assert mart.invalidations == 1
    for component in REQUIRED_COMPONENTS:
        assert mart.digest("serving_stage_named_db", f"{component}_live") == f"old-{component}"
    assert ledger.rollback_events("run2")[0].actor == "test-operator"


def test_zero_row_backup_is_rejected_before_any_rename() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_complete_generation(ledger, mart)
    mart.add("fdm_live__old_run2", 0, "empty")

    with pytest.raises(RuntimeError, match="empty rollback backup"):
        build_rollback_plan(ledger, mart, target="run2", serving_db="serving_stage_named_db")

    assert mart.rename_calls == []


def test_partial_component_set_is_rejected() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_complete_generation(ledger, mart)
    ledger.delete_component_for_test("run2", "fdm")

    with pytest.raises(RuntimeError, match="incomplete promotion set.*fdm"):
        build_rollback_plan(ledger, mart, target="run2", serving_db="serving_stage_named_db")


@pytest.mark.parametrize("serving_db", ["jw_mart_stage", "jw_mart_bak", "old_stage_bak"])
def test_retention_never_selects_runtime_serving_db_by_name(serving_db: str) -> None:
    ledger = _ledger()
    ledger.record_generation("r1", "2026-01", "job1", "generation_a", "generation_a")
    ledger.record_generation("r2", "2026-02", "job2", serving_db, serving_db)
    ledger.record_generation("r3", "2026-03", "job3", "generation_c", "generation_c")

    plan = build_retention_plan(
        ledger,
        serving_db=serving_db,
        keep_generation_count=2,
        keep_backup_run_count=3,
    )

    assert serving_db not in plan.generation_candidates
    assert plan.protected_serving_db == serving_db


def test_epoch_reverse_lookup_returns_promoted_generation() -> None:
    ledger = _ledger()
    ledger.record_generation("promotion-7", "2026-03", "ingest-42", "serving", "generation-7")

    generation = ledger.generation_for_epoch("2026-03")

    assert generation is not None
    assert generation.promotion_run_id == "promotion-7"
    assert generation.ingest_run_id == "ingest-42"
    assert generation.generation_db == "generation-7"


def test_publish_backups_are_recorded_with_reverse_epoch_mapping() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    mart.add("general_live__old_run2", 17, "old-general")

    record_component_backups(
        ledger,
        mart,
        identity=PromotionIdentity(
            promotion_run_id="run2",
            epoch="2026-03",
            ingest_run_id="job-202603",
            serving_db="serving_stage_named_db",
            generation_db="generation-2",
        ),
        component="general",
        table_pairs=(("general_live", "general_live__old_run2"),),
    )

    assert ledger.generation_for_epoch("2026-03").promotion_run_id == "run2"
    backup = ledger.components("run2")["general"][0]
    assert backup.expected_rows == 17
    assert backup.expected_digest == "old-general"

    record_component_backups(
        ledger,
        mart,
        identity=PromotionIdentity(
            promotion_run_id="run2",
            epoch="2026-03",
            ingest_run_id="job-202603",
            serving_db="serving_stage_named_db",
            generation_db="generation-2",
        ),
        component="general",
        table_pairs=(("general_live", "general_live__old_run2"),),
    )


def test_publish_backup_identity_drift_is_rejected() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    identity = PromotionIdentity("run2", "2026-03", "job-202603", "serving", "generation-2")
    mart.add("general_old", 17, "first")
    record_component_backups(
        ledger,
        mart,
        identity=identity,
        component="general",
        table_pairs=(("general_live", "general_old"),),
    )
    mart.add("general_old", 18, "changed")

    with pytest.raises(RuntimeError, match="component identity conflict"):
        record_component_backups(
            ledger,
            mart,
            identity=identity,
            component="general",
            table_pairs=(("general_live", "general_old"),),
        )


def test_dry_run_is_default_and_never_mutates() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_complete_generation(ledger, mart)
    plan = build_rollback_plan(ledger, mart, target="latest-good", serving_db="serving_stage_named_db")

    result = execute_rollback(ledger, mart, plan, actor="tester", reason="preview")

    assert result.changed is False
    assert mart.rename_calls == []
    assert mart.invalidations == 0


def test_yes_is_required_even_when_dry_run_flag_is_false() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_complete_generation(ledger, mart)
    plan = build_rollback_plan(ledger, mart, target="run2", serving_db="serving_stage_named_db")

    with pytest.raises(RuntimeError, match="--yes"):
        execute_rollback(ledger, mart, plan, actor="tester", reason="unsafe", dry_run=False)

    assert mart.rename_calls == []
