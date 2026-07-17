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


def test_ingest_manifests_pin_the_default_job_image():
    """Internal identity: every tracked ingest manifest runs config.DEFAULT_JOB_IMAGE.

    (The poll CronJob keeps its own ED-round pin; ingest upgrades independently.)"""
    base = REPO_ROOT / "deploy" / "k8s" / "ingest-hook"
    for name in ("ingest-trigger-deployment.yaml", "ingest-job-template.yaml", "ingest-sweep-cronjob.yaml"):
        assert config.DEFAULT_JOB_IMAGE in (base / ("reference/" + name) if name == "ingest-job-template.yaml" else base / name).read_text(encoding="utf-8"), name


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


def test_rendered_job_inherits_env_and_secret_refs(monkeypatch):
    monkeypatch.setenv("MARIADB_HOST", "db.example")
    monkeypatch.setenv("INGEST_S3_BUCKET", "jw-market-raw")
    monkeypatch.setenv("INGEST_REHEARSAL_ROOT", "/tmp/ingest-rehearsal")
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="_manifests/m.json", namespace="llmops")
    env = body["spec"]["template"]["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e for e in env}
    assert by_name["MARIADB_HOST"]["value"] == "db.example"
    assert by_name["INGEST_REHEARSAL_ROOT"]["value"] == "/tmp/ingest-rehearsal"
    assert by_name["MARIADB_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "jw-mart-d2-writer"
    assert by_name["INGEST_S3_BUCKET"]["valueFrom"]["secretKeyRef"]["key"] == "MINIO_MARKET_BUCKET"
    assert by_name["MINIO_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "jw-data-portal-secrets"


def test_rendered_job_env_minimal_without_s3(monkeypatch):
    for name in ("INGEST_S3_BUCKET", "MARIADB_HOST", "INGEST_REHEARSAL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="/m.json", namespace="llmops")
    names = [e["name"] for e in body["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert "INGEST_S3_BUCKET" not in names and "MARIADB_USER" in names
