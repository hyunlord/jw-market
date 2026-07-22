from __future__ import annotations

from pathlib import Path

from pipeline.scripts.crawler.crawl_temporal_contract import ACTIVITY_POLICIES


def test_activity_policies_cover_every_stage_with_measured_timeouts() -> None:
    assert ACTIVITY_POLICIES["capture_exposure_baseline"].start_to_close_seconds == 900
    assert ACTIVITY_POLICIES["tier1_collect"].start_to_close_seconds == 10_800
    assert ACTIVITY_POLICIES["tier1_classify"].start_to_close_seconds == 1_800
    assert ACTIVITY_POLICIES["tier2_collect"].start_to_close_seconds == 28_800
    assert ACTIVITY_POLICIES["tier2_classify_and_refresh"].start_to_close_seconds == 7_200
    assert all(policy.heartbeat_seconds <= 300 for policy in ACTIVITY_POLICIES.values())
    assert all(policy.maximum_attempts == 2 for policy in ACTIVITY_POLICIES.values())


def test_temporal_runtime_is_sequential_and_validation_is_non_retryable() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "pipeline/scripts/crawler/temporal_crawl_daily.py").read_text(
        encoding="utf-8"
    )

    assert "for stage in ACTIVITY_STAGES" in source
    assert "await workflow.execute_activity" in source
    assert "non_retryable=True" in source
    assert "activity.heartbeat" in source
    assert "asyncio.create_subprocess_exec" in source
    assert "capture_exposure_baseline" in source
    assert "asyncio.to_thread" in source
    assert '"stage_timeout"' in source


def test_shadow_manifest_cannot_create_a_schedule_or_cronjob() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "kind: Deployment" in manifest
    assert "kind: CronJob" not in manifest
    assert "kind: Schedule" not in manifest
    assert "-canonical" not in manifest
    assert "jw-market-crawl-temporal-shadow-v1" in manifest
