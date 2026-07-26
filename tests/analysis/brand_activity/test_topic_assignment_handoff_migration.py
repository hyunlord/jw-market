from __future__ import annotations

from pathlib import Path

from pipeline.scripts.analysis.brand_activity.auto_topic import (
    topic_assignment_handoff_db as handoff_db,
)
from pipeline.scripts.analysis.brand_activity.auto_topic import (
    topic_assignment_handoff_migration as migration,
)


def _normalized(sql: str) -> str:
    return " ".join(sql.rstrip().rstrip(";").split())


def test_committed_migration_matches_runtime_handoff_ddl() -> None:
    """The production migration and producer-side CREATE contract must not drift."""
    migration_sql = migration.MIGRATION_PATH.read_text(encoding="utf-8")
    active_sql = migration.active_migration_sql(migration_sql)
    runtime_sql = handoff_db.handoff_table_ddl(
        "jw_brand_activity_stage",
        handoff_db.HANDOFF_TABLE,
    )

    assert _normalized(active_sql) == _normalized(runtime_sql)
    assert "CREATE TABLE IF NOT EXISTS" in active_sql
    assert (
        "KEY idx_topic_assignment_handoff_pending "
        "(axis_status, assignment_status, created_at, run_id)"
    ) in active_sql


def test_migration_is_additive_and_rollback_is_comment_only() -> None:
    """Only the new table may be created; rollback remains an operator note."""
    migration_sql = migration.MIGRATION_PATH.read_text(encoding="utf-8")
    validation = migration.validate_migration_sql(migration_sql)

    assert validation.table == handoff_db.HANDOFF_TABLE
    assert validation.active_destructive_statements == ()
    assert validation.data_statements == ()
    assert (
        "-- Rollback (record only; never run automatically):"
        in migration_sql
    )
    assert (
        "DROP TABLE `jw_brand_activity_stage`."
        "`mart_brand_activity_assignment_handoff`;"
        in migration_sql
    )


def test_dry_run_validates_without_connecting(
    monkeypatch,
    capsys,
) -> None:
    """Dry-run proves the committed artifact without opening a DB connection."""
    monkeypatch.setattr(
        migration,
        "connect_from_env",
        lambda: (_ for _ in ()).throw(AssertionError("must not connect")),
    )

    assert migration.main(["--dry-run"]) == 0
    assert "migration_dry_run=PASS" in capsys.readouterr().out


def test_migration_path_is_domain_local_and_numbered() -> None:
    assert migration.MIGRATION_PATH == (
        Path(migration.__file__).with_name("migrations")
        / "001_create_assignment_handoff.sql"
    )
