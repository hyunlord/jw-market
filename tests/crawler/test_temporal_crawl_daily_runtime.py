from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from pipeline.scripts.crawler.crawl_temporal_contract import (
    ACTIVITY_POLICIES,
    WORKFLOW_EXECUTION_TIMEOUT_SECONDS,
)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_activity_policies_cover_every_stage_with_measured_timeouts() -> None:
    assert ACTIVITY_POLICIES["capture_exposure_baseline"].start_to_close_seconds == 900
    assert ACTIVITY_POLICIES["tier1_collect"].start_to_close_seconds == 10_800
    assert ACTIVITY_POLICIES["tier1_classify"].start_to_close_seconds == 1_800
    assert ACTIVITY_POLICIES["tier2_collect"].start_to_close_seconds == 57_600
    assert ACTIVITY_POLICIES["tier2_classify_and_refresh"].start_to_close_seconds == 7_200
    assert all(policy.heartbeat_seconds <= 300 for policy in ACTIVITY_POLICIES.values())
    assert {
        stage: policy.maximum_attempts
        for stage, policy in ACTIVITY_POLICIES.items()
    } == {
        "capture_exposure_baseline": 2,
        "tier1_collect": 1,
        "tier1_classify": 2,
        "tier2_collect": 1,
        "tier2_classify_and_refresh": 1,
    }


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
    assert "resolve_execution_config(config, temporal_run_id=workflow.info().run_id)" in source
    assert "start_new_session=True" in source
    assert "os.killpg" in source


def test_manual_workflow_start_allows_the_full_daily_window() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "pipeline/scripts/crawler/temporal_crawl_start.py").read_text(
        encoding="utf-8"
    )

    assert (
        "execution_timeout=timedelta(seconds=WORKFLOW_EXECUTION_TIMEOUT_SECONDS)"
        in source
    )
    assert WORKFLOW_EXECUTION_TIMEOUT_SECONDS == 86_400
    assert (
        sum(
            policy.start_to_close_seconds * policy.maximum_attempts
            for policy in ACTIVITY_POLICIES.values()
        )
        < WORKFLOW_EXECUTION_TIMEOUT_SECONDS
    )


def test_temporal_cleanup_terminates_descendant_processes(tmp_path: Path) -> None:
    pytest.importorskip("temporalio")
    from pipeline.scripts.crawler.temporal_crawl_daily import _terminate_process

    async def scenario() -> None:
        # Given: a Temporal stage process owns a long-running Python descendant.
        child_pid_path = tmp_path / "child.pid"
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            (
                f'"{sys.executable}" -c '
                f"'import os,time; "
                f'open(\"{child_pid_path}\", \"w\").write(str(os.getpid())); '
                "time.sleep(60)' & wait"
            ),
            start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if child_pid_path.is_file():
                    break
                await asyncio.sleep(0.02)
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))

            # When: Temporal cancellation invokes the worker cleanup boundary.
            await _terminate_process(process)

            # Then: both the stage shell and its descendant are gone.
            assert process.returncode is not None
            deadline = time.monotonic() + 2
            while _pid_exists(child_pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not _pid_exists(child_pid)
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    asyncio.run(scenario())


def test_temporal_cleanup_does_not_signal_an_exited_process(monkeypatch) -> None:
    pytest.importorskip("temporalio")
    from pipeline.scripts.crawler.temporal_crawl_daily import _terminate_process

    async def scenario() -> None:
        # Given: the stage exited before cancellation cleanup acquired control.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
        )
        await process.wait()

        def unexpected_signal(_process_group_id: int, _signal: int) -> None:
            raise AssertionError("an exited process ID must not be signalled")

        monkeypatch.setattr(os, "killpg", unexpected_signal)

        # When: cleanup receives the completed process.
        await _terminate_process(process)

        # Then: the completed result remains intact without signalling its old PID.
        assert process.returncode == 0

    asyncio.run(scenario())


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


def test_shadow_manifest_uses_an_isolated_state_claim() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "jw-crawl-temporal-shadow-state" in manifest
    assert "jw-crawl-chain-state" not in manifest


def test_shadow_manifest_disables_tier2_llm_calls() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: CRAWL_CHAIN_LLM_CALL_LIMIT" in manifest
    assert 'value: "0"' in manifest


def test_shadow_manifest_uses_the_live_tier1_classifier_endpoint() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: WF196_DIRECT_RUN_URL" in manifest
    assert "http://workflow-196.llmops.svc.cluster.local:8080/run/v2" in manifest


def test_production_worker_is_separate_and_unbounded() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "deploy/k8s/crawler/temporal-crawl-worker.yaml"

    assert path.is_file()
    manifest = path.read_text(encoding="utf-8")
    assert "name: jw-market-crawl-temporal-worker" in manifest
    assert "name: jw-crawl-temporal-state" in manifest
    assert "value: jw-market-crawl-temporal-v1" in manifest
    assert "name: CRAWL_CHAIN_LLM_CALL_LIMIT" in manifest
    assert 'value: "100"' in manifest
    assert "name: CRAWL_CHAIN_LLM_MAX_COST_KRW" in manifest
    assert 'value: "339.00"' in manifest
    assert "shadow" not in manifest.lower()
    assert "CRAWL_CHAIN_TIER1_SITES" not in manifest
    assert "CRAWL_CHAIN_TIER1_MAX_ARTICLES" not in manifest
    assert "CRAWL_CHAIN_TIER2_SITES" not in manifest
    assert "CRAWL_CHAIN_TIER2_DAYS" not in manifest
    assert "CRAWL_CHAIN_TIER2_MAX_PAGES_PER_SITE" not in manifest
    assert "CRAWL_CHAIN_TIER2_MAX_LINKS_PER_PAGE" not in manifest
    assert "CRAWL_CHAIN_TIER2_MAX_ARTICLES" not in manifest
    assert "CRAWL_CHAIN_TIER2_LIMIT_BRANDS" not in manifest
