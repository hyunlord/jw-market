from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.agent_refresh_weekly.contract import (
    STAGE_ORDER,
    classify_job_status,
    find_active_conflicts,
    make_job_name,
    render_stage_job,
)


IMAGE = (
    "asia-northeast3-docker.pkg.dev/example/project/jw-pipeline-orchestrator"
    "@sha256:" + "a" * 64
)


def _job(name: str, *, active: int = 1, succeeded: int = 0, failed: int = 0) -> dict:
    return {
        "metadata": {"name": name},
        "status": {"active": active, "succeeded": succeeded, "failed": failed},
    }


def test_stage_order_runs_agent2_then_agent3() -> None:
    assert STAGE_ORDER == ("agent2", "agent3")


def test_job_name_is_deterministic_and_dns_bounded() -> None:
    first = make_job_name("schedule/weekly run with a very long id" * 4, "agent2")
    second = make_job_name("schedule/weekly run with a very long id" * 4, "agent2")

    assert first == second
    assert first.startswith("jw-agent-refresh-weekly-agent2-")
    assert len(first) <= 63
    assert first != make_job_name("schedule/weekly run with a very long id" * 4, "agent3")


def test_agent2_job_is_global_staging_only_and_visible_to_ingest_cap() -> None:
    body = render_stage_job(
        stage="agent2",
        workflow_id="weekly-test",
        image=IMAGE,
        namespace="llmops",
        output_claim="llmops-market-output",
    )
    container = body["spec"]["template"]["spec"]["containers"][0]
    script = container["args"][0]

    assert body["metadata"]["labels"]["app"] == "jw-agent-refresh"
    assert body["spec"]["backoffLimit"] == 0
    assert body["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert script.count("pipeline.scripts.ai_analysis.agent2_regen_orchestrator") == 2
    assert script.count("--dry-run") == 2
    assert "--analysis-variant short" in script
    assert "--analysis-variant long" in script
    assert "--brands" not in script
    assert "affected_scope" not in script
    assert "/market-output/agent-refresh-weekly/${WEEKLY_RUN_ID}" in script


def test_agent3_job_is_global_and_keeps_revision_pin() -> None:
    body = render_stage_job(
        stage="agent3",
        workflow_id="weekly-test",
        image=IMAGE,
        namespace="llmops",
        output_claim="llmops-market-output",
    )
    container = body["spec"]["template"]["spec"]["containers"][0]
    script = container["args"][0]

    assert "pipeline.scripts.agent3.run_source" in script
    assert "--source all" in script
    assert "--expected-workflow-rev 5692" in script
    assert "--brands" not in script
    assert "affected_scope" not in script


def test_job_image_must_be_an_immutable_digest() -> None:
    with pytest.raises(ValueError, match="immutable digest"):
        render_stage_job(
            stage="agent2",
            workflow_id="weekly-test",
            image="example.invalid/agent:latest",
            namespace="llmops",
            output_claim="llmops-market-output",
        )


def test_active_conflict_detection_covers_ingest_and_all_agent_paths() -> None:
    jobs = [
        _job("jw-ingest-ubist-abc"),
        _job("jw-ingest-publish-ubist-abc"),
        _job("jw-agent-refresh-ubist-abc"),
        _job("jw-agent3-refresh-daily-123"),
        _job("unrelated-active-job"),
        _job("jw-ingest-complete", active=0, succeeded=1),
    ]

    assert find_active_conflicts(jobs) == (
        "jw-agent-refresh-ubist-abc",
        "jw-agent3-refresh-daily-123",
        "jw-ingest-publish-ubist-abc",
        "jw-ingest-ubist-abc",
    )


def test_active_conflict_detection_excludes_the_owned_stage_job() -> None:
    owned = make_job_name("weekly-test", "agent2")
    assert find_active_conflicts([_job(owned)], owned_job=owned) == ()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": {"active": 1}}, "Running"),
        ({"status": {"succeeded": 1}}, "Complete"),
        (
            {
                "status": {
                    "failed": 1,
                    "conditions": [{"type": "Failed", "status": "True"}],
                }
            },
            "Failed",
        ),
        ({"status": {"active": 1, "failed": 1}}, "Running"),
        ({"status": {"failed": 1}}, "Pending"),
        ({"status": {}}, "Pending"),
    ],
)
def test_job_status_is_explicit(payload: dict, expected: str) -> None:
    assert classify_job_status(payload) == expected


def test_deployment_and_schedule_contracts_are_additive_and_weekly() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (
        root / "deploy/k8s/agent-refresh-weekly/agent-refresh-temporal-worker.yaml"
    ).read_text(encoding="utf-8")
    schedule = (
        root / "deploy/temporal/create-agent2-agent3-weekly-schedule.sh"
    ).read_text(encoding="utf-8")

    assert "name: jw-agent-refresh-temporal-worker" in manifest
    assert "name: jw-agent-refresh-temporal" in manifest
    assert "jw-ingest-hook" not in manifest
    assert "jw-market-backend-api" not in manifest
    assert "jw-agent-refresh-weekly-v1" in manifest
    assert "@sha256:" in manifest
    assert "@sha256:" + "0" * 64 not in manifest
    assert "name: APP_VERSION" in manifest
    assert "value: cc509795d3731e5be6a09dc7d3da24af8cc0d2ce" in manifest
    assert "--schedule-id jw-agent2-agent3-weekly-v1" in schedule
    assert "--address \"${TEMPORAL_ADDRESS:-temporal-frontend.temporal.svc:7233}\"" in schedule
    assert "--cron '30 12 * * Sat'" in schedule
    assert "--time-zone Asia/Seoul" in schedule
    assert "--overlap-policy Skip" in schedule
    assert "--pause-on-failure" in schedule
    assert "--execution-timeout 10h" in schedule


def test_worker_dockerfile_only_copies_existing_package_paths() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (
        root / "deploy/docker/agent-refresh-temporal.Dockerfile"
    ).read_text(encoding="utf-8")

    assert "COPY pipeline/__init__.py /app/pipeline/__init__.py" in dockerfile
    assert "COPY pipeline/scripts/agent_refresh_weekly" in dockerfile
    assert "COPY pipeline/scripts/__init__.py" not in dockerfile
