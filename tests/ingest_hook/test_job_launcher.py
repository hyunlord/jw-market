"""Job rendering/submission: image pin, G3-first command, labels, RBAC scope."""
from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.job_launcher import render_job, submit_job

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = "f" * 64


def test_rendered_job_pins_orchestrator_image_and_runner():
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="/data/m.json", namespace="llmops")
    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == config.DEFAULT_JOB_IMAGE
    assert container["command"][:4] == ["python", "-m", "pipeline.scripts.ingest_hook.job_runner", "--manifest"]
    assert body["metadata"]["name"] == f"jw-ingest-ubist-{SHA[:8]}"
    assert body["spec"]["backoffLimit"] == 0
    assert body["metadata"]["labels"]["jw-ingest/category"] == "ubist"


def test_submit_uses_injected_transport(fake_transport):
    name = submit_job(
        category="iqvia", manifest_sha=SHA, manifest_path="/data/m.json",
        transport=fake_transport, namespace="llmops",
    )
    assert name == f"jw-ingest-iqvia-{SHA[:8]}"
    (url_path, body), = fake_transport.submitted
    assert url_path == "/apis/batch/v1/namespaces/llmops/jobs"
    assert body["metadata"]["name"] == name


def test_job_image_default_matches_orchestrator_cronjob_pin():
    """One code identity: ingest Jobs run the exact digest the orchestrator poll chain runs."""
    cronjob = REPO_ROOT / "deploy" / "k8s" / "orchestrator" / "pipeline-orchestrator-poll-cronjob.yaml"
    assert config.DEFAULT_JOB_IMAGE in cronjob.read_text(encoding="utf-8")


def test_tracked_manifests_stay_inert():
    """Repo canon ships un-activated: replicas 0 / suspend true (STOP ②)."""
    base = REPO_ROOT / "deploy" / "k8s" / "ingest-hook"
    deployment = list(yaml.safe_load_all((base / "ingest-trigger-deployment.yaml").read_text(encoding="utf-8")))
    assert deployment[0]["spec"]["replicas"] == 0
    sweep = yaml.safe_load((base / "ingest-sweep-cronjob.yaml").read_text(encoding="utf-8"))
    assert sweep["spec"]["suspend"] is True
    rbac = list(yaml.safe_load_all((base / "ingest-hook-rbac.yaml").read_text(encoding="utf-8")))
    role = next(doc for doc in rbac if doc["kind"] == "Role")
    assert role["rules"] == [{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list"]}]
