from __future__ import annotations

from pathlib import Path

from pipeline.scripts.crawler.hira_benefit.contract import (
    ACTIVITY_STAGES,
    POST_DISCOVERY_STAGES,
)


def test_workflow_is_sequential_and_has_no_schedule_creation() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (
        root
        / "pipeline/scripts/crawler/hira_benefit/temporal_workflow.py"
    ).read_text(encoding="utf-8")

    assert "for stage in POST_DISCOVERY_STAGES" in source
    assert "await workflow.execute_activity" in source
    assert "create_schedule" not in source
    assert "ScheduleClient" not in source
    assert ACTIVITY_STAGES == (
        "discover_probe",
        "discover_page_batch",
        "discover_reduce",
        "collect_details",
        "persist_results",
        "verify_run",
    )
    assert POST_DISCOVERY_STAGES == (
        "collect_details",
        "persist_results",
        "verify_run",
    )


def test_page_batches_fan_out_sequentially_and_never_run_concurrently() -> None:
    """Splitting bounds each activity; it must not raise the HIRA request rate."""

    root = Path(__file__).resolve().parents[3]
    source = (
        root
        / "pipeline/scripts/crawler/hira_benefit/temporal_workflow.py"
    ).read_text(encoding="utf-8")

    assert "for page_start, page_end in page_batches(" in source
    assert "asyncio.gather" not in source
    assert "workflow.start_activity" not in source


def test_runtime_starts_a_new_process_group() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (
        root / "pipeline/scripts/crawler/hira_benefit/runtime.py"
    ).read_text(encoding="utf-8")

    assert "start_new_session=True" in source
    assert "os.killpg" in source
    assert "signal.SIGTERM" in source
    assert "signal.SIGKILL" in source
