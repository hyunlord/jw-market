from __future__ import annotations

import sys
from datetime import timedelta
from functools import partial

import pytest

pytest.importorskip("temporalio")

from temporalio.client import ScheduleActionStartWorkflow, ScheduleOverlapPolicy

from pipeline.scripts.crawler.hira_benefit import schedule as schedule_module
from pipeline.scripts.crawler.hira_benefit.schedule import (
    SCHEDULE_RUN_ID,
    build_daily_schedule,
    scheduled_workflow_input,
)
from pipeline.scripts.crawler.hira_benefit.temporal_workflow import (
    TASK_QUEUE,
    WORKFLOW_NAME,
    resolve_run_config,
)


def test_scheduled_run_uses_temporal_workflow_id_for_durable_receipts() -> None:
    config = scheduled_workflow_input(
        state_root="/var/lib/jw-hira-benefit",
        notice_date_boundary="2026-06-24",
    )

    resolved = resolve_run_config(
        config,
        workflow_id="jw-hira-benefit-daily-v1-run-2026-07-27T01:30:00+09:00",
    )

    assert config.run_id == SCHEDULE_RUN_ID
    assert resolved.run_id == ("jw-hira-benefit-daily-v1-run-2026-07-27T01:30:00+09:00")
    assert resolved.notice_date_boundary == "2026-06-24"


def test_manual_run_id_is_not_rewritten() -> None:
    config = scheduled_workflow_input(
        state_root="/var/lib/jw-hira-benefit",
        notice_date_boundary="2026-06-24",
        run_id="manual-hira-check",
    )

    resolved = resolve_run_config(config, workflow_id="temporal-generated-id")

    assert resolved.run_id == "manual-hira-check"


def test_daily_schedule_is_skip_overlap_and_sixty_minute_incremental() -> None:
    config = scheduled_workflow_input(
        state_root="/var/lib/jw-hira-benefit",
        notice_date_boundary="2026-06-24",
    )

    schedule = build_daily_schedule(config, cron_expression="30 1 * * *")

    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    assert schedule.action.workflow == WORKFLOW_NAME
    assert schedule.action.task_queue == TASK_QUEUE
    assert schedule.action.execution_timeout == timedelta(minutes=60)
    assert schedule.spec.cron_expressions == ["30 1 * * *"]
    assert schedule.spec.time_zone_name == "Asia/Seoul"
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert config.first_run_mode == "date_boundary"
    assert config.manifest_path is None


def test_schedule_cli_binds_create_arguments_before_entering_async_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[partial[None]] = []
    monkeypatch.setattr(
        schedule_module.anyio,
        "run",
        lambda operation: captured.append(operation),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "schedule.py",
            "--temporal-address",
            "temporal.example:7233",
            "--temporal-namespace",
            "llmops",
            "--cron",
            "30 1 * * *",
            "--state-root",
            "/var/lib/jw-hira-benefit",
            "--notice-date-boundary",
            "2026-06-24",
        ],
    )

    schedule_module.main()

    assert len(captured) == 1
    assert isinstance(captured[0], partial)
    assert captured[0].keywords == {
        "temporal_address": "temporal.example:7233",
        "temporal_namespace": "llmops",
        "cron_expression": "30 1 * * *",
        "state_root": "/var/lib/jw-hira-benefit",
        "notice_date_boundary": "2026-06-24",
    }
