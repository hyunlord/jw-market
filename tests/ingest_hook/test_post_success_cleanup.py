from __future__ import annotations

import sqlite3

import pytest

from pipeline.scripts.ingest_hook import post_success_cleanup as cleanup
from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import TableBackup


class RecordingExecutor:
    def __init__(self) -> None:
        self.schemas: list[str] = []
        self.tables: list[tuple[str, tuple[str, ...]]] = []
        self.sleeps: list[float] = []

    def drop_schema(self, schema: str) -> None:
        self.schemas.append(schema)

    def drop_tables(self, schema: str, tables: tuple[str, ...]) -> None:
        self.tables.append((schema, tables))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _ledger() -> PromotionLedger:
    conn = sqlite3.connect(":memory:")
    ledger = PromotionLedger(conn, dialect="sqlite")
    ledger.ensure_tables()
    ledger.record_component(
        promotion_run_id="run-new",
        component="numeric_mart",
        epoch="2026-06",
        ingest_run_id="ingest-new",
        target_db="serving",
        generation_db="build_new",
        tables=(
            TableBackup("mart_general_brand_metric", "mart_general_brand_metric__old_run_new", 10, "10:a:b"),
        ),
    )
    ledger.record_component(
        promotion_run_id="run-old",
        component="numeric_mart",
        epoch="2026-05",
        ingest_run_id="ingest-old",
        target_db="serving",
        generation_db="build_old",
        tables=(
            TableBackup("mart_general_brand_metric", "mart_general_brand_metric__old_run_old", 10, "10:a:b"),
        ),
    )
    ledger.record_component(
        promotion_run_id="run-ancient",
        component="numeric_mart",
        epoch="2026-04",
        ingest_run_id="ingest-ancient",
        target_db="serving",
        generation_db="build_ancient",
        tables=(
            TableBackup("mart_general_brand_metric", "mart_general_brand_metric__old_run_ancient", 10, "10:a:b"),
        ),
    )
    return ledger


def test_cleanup_keeps_active_and_one_rollback_generation(tmp_path) -> None:
    ledger = _ledger()
    plan = cleanup.build_retention_cleanup_plan(
        ledger,
        serving_db="serving",
        source="ubist",
        run_id="run-new",
        keep_verified_rollback_generations=1,
        size_lookup=lambda target: 1,
    )

    assert [target.name for target in plan.targets] == ["build_ancient", "mart_general_brand_metric__old_run_ancient"]
    assert plan.total_estimated_bytes == 2
    assert plan.retained_generations == ("build_new", "build_old", "serving")


def test_cleanup_rejects_serving_schema_as_drop_target() -> None:
    with pytest.raises(cleanup.CleanupSafetyError, match="serving schema"):
        cleanup.CleanupTarget("schema", "serving", None, 1).validate(serving_db="serving")


def test_cleanup_rejects_non_strict_backup_table_name() -> None:
    with pytest.raises(cleanup.CleanupSafetyError, match="strict rollback suffix"):
        cleanup.CleanupTarget("table", "serving", "mart_general_brand_metric_old", 1).validate(
            serving_db="serving"
        )


def test_cleanup_dry_run_records_but_does_not_drop(tmp_path) -> None:
    plan = cleanup.CleanupPlan(
        source="ubist",
        run_id="run-new",
        serving_db="serving",
        retained_generations=("build_new", "serving"),
        targets=(
            cleanup.CleanupTarget("schema", "build_old", None, 1),
            cleanup.CleanupTarget("table", "serving", "mart_general_brand_metric__old_run_old", 1),
        ),
        total_estimated_bytes=2,
    )
    executor = RecordingExecutor()

    result = cleanup.execute_cleanup_plan(
        plan,
        executor=executor,
        evidence_dir=tmp_path,
        dry_run=True,
        max_drop_bytes=10,
        disk_usage_pct=lambda: 10,
        toi_interval_seconds=0.25,
    )

    assert result.dry_run is True
    assert result.dropped == ()
    assert executor.schemas == []
    assert executor.tables == []
    assert result.plan_sha256
    assert result.plan_path.read_text(encoding="utf-8")


def test_cleanup_blocks_serving_reference_before_drop(tmp_path) -> None:
    plan = cleanup.CleanupPlan(
        source="ubist",
        run_id="run-new",
        serving_db="serving",
        retained_generations=("build_new", "serving"),
        targets=(cleanup.CleanupTarget("schema", "serving", None, 1),),
        total_estimated_bytes=1,
    )

    with pytest.raises(cleanup.CleanupSafetyError, match="serving schema"):
        cleanup.execute_cleanup_plan(
            plan,
            executor=RecordingExecutor(),
            evidence_dir=tmp_path,
            dry_run=True,
            max_drop_bytes=10,
            disk_usage_pct=lambda: 10,
        )


def test_cleanup_blocks_delete_size_cap(tmp_path) -> None:
    plan = cleanup.CleanupPlan(
        source="ubist",
        run_id="run-new",
        serving_db="serving",
        retained_generations=("build_new", "serving"),
        targets=(cleanup.CleanupTarget("schema", "build_old", None, 99),),
        total_estimated_bytes=99,
    )

    with pytest.raises(cleanup.CleanupSafetyError, match="exceeds cleanup cap"):
        cleanup.execute_cleanup_plan(
            plan,
            executor=RecordingExecutor(),
            evidence_dir=tmp_path,
            dry_run=False,
            max_drop_bytes=10,
            disk_usage_pct=lambda: 10,
        )


def test_cleanup_blocks_runtime_disk_threshold_after_first_drop(tmp_path) -> None:
    plan = cleanup.CleanupPlan(
        source="ubist",
        run_id="run-new",
        serving_db="serving",
        retained_generations=("build_new", "serving"),
        targets=(
            cleanup.CleanupTarget("schema", "build_old", None, 1),
            cleanup.CleanupTarget("schema", "build_older", None, 1),
        ),
        total_estimated_bytes=2,
    )
    readings = iter([10, 10, 81])
    executor = RecordingExecutor()

    with pytest.raises(cleanup.CleanupSafetyError, match="runtime disk usage"):
        cleanup.execute_cleanup_plan(
            plan,
            executor=executor,
            evidence_dir=tmp_path,
            dry_run=False,
            max_drop_bytes=10,
            disk_usage_pct=lambda: next(readings),
        )
    assert executor.schemas == ["build_old"]


def test_complete_reingest_failure_path_does_not_run_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from pipeline.scripts.ingest_hook import complete_reingest_runner as runner

    calls: list[str] = []
    context = runner.RequestContext(
        identity=("2026-Q1", "ubist", "a" * 64),
        run_id="run-new",
        category="ubist",
        request_id="request",
        parent_run_id="parent",
        affected_scope={"dimension": "source", "count": 1, "values": ["ubist"]},
        scope_values=None,
        period_scope=None,
    )
    monkeypatch.setattr(
        runner,
        "_publish_and_refresh_numeric",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )
    monkeypatch.setattr(runner, "_run_post_success_cleanup", lambda *_args, **_kwargs: calls.append("cleanup"))

    class Ledger:
        def record_stage(self, *_args, **_kwargs) -> None:
            return None

        def record_complete_reingest_terminal(self, *_args, **_kwargs) -> bool:
            return True

    with pytest.raises(RuntimeError, match="publish failed"):
        try:
            runner._publish_and_refresh_numeric(
                context,
                Ledger(),
                runner.PreparedMart("serving", "build", ("table",)),
                object(),
            )
        finally:
            assert calls == []


def test_job_runner_cleanup_happens_before_completion_signal() -> None:
    from pathlib import Path

    source = Path("pipeline/scripts/ingest_hook/job_runner.py").read_text(encoding="utf-8")
    cleanup_index = source.index('if mode == "production" and published_target_schema:')
    signal_index = source.index("activation_signal = _emit_completion_signal(")

    assert cleanup_index < signal_index
