from __future__ import annotations

from argparse import Namespace
import sqlite3

import pymysql
import pytest

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import REQUIRED_COMPONENTS, TableBackup
from pipeline.scripts.rollback.planner import build_retention_plan, build_rollback_plan
from pipeline.scripts.rollback.__main__ import parse_args
from pipeline.scripts.rollback.recording import (
    PromotionIdentity,
    identity_from_args,
    record_component_backups,
    require_ingest_post_gate,
)
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


def test_gate_failed_ingest_blocks_promotion() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ingest_ledger (id INTEGER, run_id TEXT, status TEXT, reason TEXT)")
    conn.execute("INSERT INTO ingest_ledger VALUES (1, 'ingest-1', 'gate_failed', 'PG-2 mismatch')")
    identity = PromotionIdentity(
        "promote-1",
        "2026-07",
        "ingest-1",
        "serving",
        "generation",
        ledger_db="main",
    )

    with pytest.raises(RuntimeError, match="status=gate_failed"):
        require_ingest_post_gate(conn, identity, dialect="sqlite")


def test_complete_ingest_allows_promotion_preflight() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ingest_ledger (id INTEGER, run_id TEXT, status TEXT, reason TEXT)")
    conn.execute("INSERT INTO ingest_ledger VALUES (1, 'ingest-1', 'complete', NULL)")
    identity = PromotionIdentity(
        "promote-1",
        "2026-07",
        "ingest-1",
        "serving",
        "generation",
        ledger_db="main",
    )

    require_ingest_post_gate(conn, identity, dialect="sqlite")


def test_post_gate_qualifies_the_explicit_ledger_database() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "ATTACH DATABASE ':memory:' AS d2"
    )
    conn.execute(
        "CREATE TABLE d2.ingest_ledger "
        "(id INTEGER, run_id TEXT, status TEXT, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO d2.ingest_ledger VALUES "
        "(1, 'ingest-1', 'complete', NULL)"
    )
    identity = PromotionIdentity(
        "promote-1",
        "2026-07",
        "ingest-1",
        "serving",
        "generation",
        ledger_db="d2",
    )

    require_ingest_post_gate(conn, identity, dialect="sqlite")


def test_post_gate_uses_latest_row_for_duplicate_run_id() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ingest_ledger "
        "(id INTEGER, run_id TEXT, status TEXT, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO ingest_ledger VALUES "
        "(1, 'ingest-1', 'running', NULL), "
        "(2, 'ingest-1', 'complete', NULL)"
    )
    identity = PromotionIdentity(
        "promote-1",
        "2026-07",
        "ingest-1",
        "serving",
        "generation",
        ledger_db="main",
    )

    require_ingest_post_gate(conn, identity, dialect="sqlite")


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (None, "absent from ingest_ledger"),
        (("running", None), "status=running"),
    ],
)
def test_post_gate_distinguishes_absent_and_incomplete_runs(
    row: tuple[str, str | None] | None,
    message: str,
) -> None:
    class Cursor:
        def execute(self, _sql, _params) -> None:
            return None

        def fetchone(self):
            return row

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    identity = PromotionIdentity(
        "promote-1",
        "2026-07",
        "ingest-1",
        "serving",
        "generation",
        ledger_db="ledger",
    )

    with pytest.raises(RuntimeError, match=message):
        require_ingest_post_gate(Connection(), identity)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            pymysql.err.OperationalError(
                1969,
                "Query execution was interrupted (max_statement_time exceeded)",
            ),
            "post-gate statement timed out",
        ),
        (
            pymysql.err.OperationalError(
                2013,
                "Lost connection to MySQL server during query (timed out)",
            ),
            "post-gate socket timed out",
        ),
    ],
)
def test_post_gate_timeout_modes_fail_closed(
    error: pymysql.err.OperationalError,
    message: str,
) -> None:
    class Cursor:
        def execute(self, sql, _params) -> None:
            assert sql.startswith("SET STATEMENT max_statement_time=5 FOR SELECT")
            raise error

        def fetchone(self):
            raise AssertionError("timed out query must not return a row")

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    identity = PromotionIdentity(
        "promote-1",
        "2026-07",
        "ingest-1",
        "serving",
        "generation",
        ledger_db="ledger",
    )

    with pytest.raises(RuntimeError, match=message):
        require_ingest_post_gate(Connection(), identity)


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


def test_partial_generation_cannot_replace_latest_good() -> None:
    ledger = _ledger()
    mart = IsolatedMart()
    _record_complete_generation(ledger, mart, run_id="complete-run")
    ledger.record_component(
        promotion_run_id="newer-partial-run",
        component="general",
        epoch="2026-04",
        ingest_run_id="job-202604",
        target_db="serving_stage_named_db",
        generation_db="generation-3",
        tables=(TableBackup("general_live", "general_old", 1, "digest"),),
    )

    latest = ledger.generation("latest-good")

    assert latest is not None
    assert latest.promotion_run_id == "complete-run"


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


def test_retention_cli_keeps_one_old_backup_run_by_default() -> None:
    action, args = parse_args(["retention", "--list", "--target-db", "serving"])

    assert action == "retention"
    assert args.keep_generations == 1
    assert args.keep_backup_runs == 1
    assert args.apply is False
    assert args.yes is False


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

    assert ledger.generation("run2").status == "building"
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
    for component in REQUIRED_COMPONENTS - {"general"}:
        ledger.record_component(
            promotion_run_id="run2",
            component=component,
            epoch="2026-03",
            ingest_run_id="job-202603",
            target_db="serving_stage_named_db",
            generation_db="generation-2",
            tables=(TableBackup(f"{component}_live", f"{component}_old", 1, "digest"),),
        )

    assert ledger.generation_for_epoch("2026-03").promotion_run_id == "run2"


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


def test_mutating_promotion_requires_ledger_identity() -> None:
    args = Namespace(
        promotion_epoch=None,
        ingest_run_id=None,
        generation_db=None,
        ledger_db=None,
    )

    with pytest.raises(ValueError, match="promotion ledger identity is required"):
        identity_from_args(
            args,
            promotion_run_id="run-new",
            serving_db="serving_stage_named_db",
            required=True,
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
