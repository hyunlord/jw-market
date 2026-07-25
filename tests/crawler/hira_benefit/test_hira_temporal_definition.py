from __future__ import annotations

from pathlib import Path

from pipeline.scripts.crawler.hira_benefit.contract import ACTIVITY_STAGES


def test_workflow_is_sequential_and_has_no_schedule_creation() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (
        root
        / "pipeline/scripts/crawler/hira_benefit/temporal_workflow.py"
    ).read_text(encoding="utf-8")

    assert "for stage in ACTIVITY_STAGES" in source
    assert "await workflow.execute_activity" in source
    assert "create_schedule" not in source
    assert "ScheduleClient" not in source
    assert ACTIVITY_STAGES == (
        "discover_changes",
        "collect_details",
        "persist_results",
        "verify_run",
    )


def test_runtime_starts_a_new_process_group() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (
        root / "pipeline/scripts/crawler/hira_benefit/runtime.py"
    ).read_text(encoding="utf-8")

    assert "start_new_session=True" in source
    assert "os.killpg" in source
    assert "signal.SIGTERM" in source
    assert "signal.SIGKILL" in source
