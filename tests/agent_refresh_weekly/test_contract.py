from __future__ import annotations

import asyncio
import ast
import sys
import types
from pathlib import Path

import pytest

from pipeline.scripts.agent_refresh_weekly.contract import (
    STAGE_ORDER,
    classify_job_status,
    find_active_conflicts,
    make_preflight_result,
    make_job_name,
    make_stage_skip_result,
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


def _import_activities(monkeypatch: pytest.MonkeyPatch):
    temporalio = types.ModuleType("temporalio")
    activity = types.SimpleNamespace(
        defn=lambda name: lambda function: function,
        heartbeat=lambda details: None,
        info=lambda: types.SimpleNamespace(attempt=1),
        logger=types.SimpleNamespace(error=lambda *args: None),
    )
    temporalio.activity = activity
    exceptions = types.ModuleType("temporalio.exceptions")

    class ApplicationError(Exception):
        pass

    exceptions.ApplicationError = ApplicationError
    monkeypatch.setitem(sys.modules, "temporalio", temporalio)
    monkeypatch.setitem(sys.modules, "temporalio.exceptions", exceptions)

    from pipeline.scripts.agent_refresh_weekly import activities

    return activities


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


def test_active_conflict_detection_covers_labelled_complete_reingest_jobs() -> None:
    reingest = _job("manual-reingest-ubist-abc")
    reingest["metadata"]["labels"] = {
        "app": "jw-complete-reingest",
        "jw.ingest/category": "ubist",
    }

    assert find_active_conflicts([reingest]) == (
        "manual-reingest-ubist-abc",
    )


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
    assert "value: 7e607e6f3416da275275c1686bce53b8bf5895ca" in manifest
    assert "kubernetes.io/change-cause:" in manifest
    assert "--schedule-id jw-agent2-agent3-weekly-v1" in schedule
    assert "--address \"${TEMPORAL_ADDRESS:-temporal-frontend.temporal.svc:7233}\"" in schedule
    assert "--cron '30 12 * * Sat'" in schedule
    assert "--time-zone Asia/Seoul" in schedule
    assert "--overlap-policy Skip" in schedule
    assert "--pause-on-failure" in schedule
    assert "--execution-timeout 10h" in schedule


def test_preflight_conflict_is_a_skip() -> None:
    assert make_preflight_result(
        workflow_id="weekly-test",
        conflicts=("jw-ingest-ubist-active",),
        galera=[],
    ) == {
        "workflow_id": "weekly-test",
        "status": "skipped",
        "reason": "active_job_conflict",
        "galera": [],
        "active_conflicts": ["jw-ingest-ubist-active"],
    }


def test_stage_conflict_result_distinguishes_cleanup() -> None:
    assert make_stage_skip_result(
        stage="agent2",
        job="jw-agent-refresh-weekly-agent2-token",
        conflicts=("jw-ingest-ubist-active",),
        owned_job_deleted=False,
    ) == {
        "stage": "agent2",
        "job": "jw-agent-refresh-weekly-agent2-token",
        "status": "skipped",
        "reason": "active_job_conflict",
        "active_conflicts": ["jw-ingest-ubist-active"],
        "owned_job_deleted": False,
    }


def test_stage_start_conflict_skips_without_creating_job(monkeypatch: pytest.MonkeyPatch) -> None:
    activities = _import_activities(monkeypatch)

    class FakeApi:
        def __init__(self, namespace: str) -> None:
            assert namespace == "llmops"

        def list_jobs(self) -> list[dict]:
            return [_job("jw-ingest-ubist-active")]

        def create_job(self, body: dict) -> dict:
            raise AssertionError("a conflicting stage must not create a Job")

    monkeypatch.setattr(activities, "KubernetesApi", FakeApi)
    result = asyncio.run(activities._run_stage("agent2", "weekly-test"))

    assert result["status"] == "skipped"
    assert result["owned_job_deleted"] is False
    assert result["active_conflicts"] == ["jw-ingest-ubist-active"]


def test_running_stage_conflict_deletes_owned_job_then_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activities = _import_activities(monkeypatch)

    owned_name = make_job_name("weekly-test", "agent2")
    owned = _job(owned_name)
    owned["metadata"].update({"uid": "owned-uid", "resourceVersion": "17"})

    class FakeApi:
        def __init__(self, namespace: str) -> None:
            self.list_calls = 0
            self.deleted: list[dict] = []

        def list_jobs(self) -> list[dict]:
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return [owned, _job("jw-agent-refresh-iqvia-nsa-active")]

        def create_job(self, body: dict) -> dict:
            assert body["metadata"]["name"] == owned_name
            return owned

        def get_job(self, name: str) -> dict:
            assert name == owned_name
            return owned

        def delete_job(self, job: dict) -> dict:
            self.deleted.append(job)
            return {"status": "Success"}

    fake_api = FakeApi("llmops")
    monkeypatch.setattr(activities, "KubernetesApi", lambda namespace: fake_api)
    monkeypatch.setattr(activities, "_AGENT_JOB_IMAGE", IMAGE)
    monkeypatch.setattr(activities.activity, "heartbeat", lambda details: None)

    result = asyncio.run(activities._run_stage("agent2", "weekly-test"))

    assert result["status"] == "skipped"
    assert result["owned_job_deleted"] is True
    assert result["active_conflicts"] == ["jw-agent-refresh-iqvia-nsa-active"]
    assert fake_api.deleted == [owned]


def test_workflow_returns_before_stages_when_preflight_is_skipped() -> None:
    root = Path(__file__).resolve().parents[2]
    worker = (
        root / "pipeline/scripts/agent_refresh_weekly/temporal_worker.py"
    ).read_text(encoding="utf-8")

    assert 'if preflight["status"] == "skipped":' in worker
    assert 'return {"status": "skipped", "preflight": preflight, "stages": []}' in worker
    assert 'if result["status"] == "skipped":' in worker
    assert 'return {"status": "skipped", "preflight": preflight, "stages": stages}' in worker


def test_worker_dockerfile_only_copies_existing_package_paths() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (
        root / "deploy/docker/agent-refresh-temporal.Dockerfile"
    ).read_text(encoding="utf-8")

    assert "COPY pipeline/__init__.py /app/pipeline/__init__.py" in dockerfile
    assert "COPY pipeline/scripts/agent_refresh_weekly" in dockerfile
    assert "COPY pipeline/scripts/__init__.py" not in dockerfile
    assert "FROM python:3.11-slim@sha256:" in dockerfile
    assert "KUBECTL_SHA256=" in dockerfile
    assert "sha256sum -c -" in dockerfile


def test_temporal_workflow_module_uses_sandbox_safe_absolute_imports() -> None:
    root = Path(__file__).resolve().parents[2]
    package = root / "pipeline/scripts/agent_refresh_weekly"
    relative_imports = {
        path.name: [
            node
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom) and node.level
        ]
        for path in sorted(package.glob("*.py"))
    }

    assert relative_imports and all(not imports for imports in relative_imports.values())
