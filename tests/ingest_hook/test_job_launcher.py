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


def test_tracked_manifests_preserve_isolated_load_arming():
    """Repo canon preserves D-3a arming without enabling production load."""
    base = REPO_ROOT / "deploy" / "k8s" / "ingest-hook"
    deployment = list(yaml.safe_load_all((base / "ingest-trigger-deployment.yaml").read_text(encoding="utf-8")))
    assert deployment[0]["spec"]["replicas"] == 1
    trigger = deployment[0]["spec"]["template"]["spec"]["containers"][0]
    trigger_env = {item["name"]: item for item in trigger["env"]}
    assert trigger_env["INGEST_LOAD_STAGING_ROOT"]["value"] == "/tmp/ingest-load-staging"
    assert "INGEST_REHEARSAL_ROOT" not in trigger_env
    assert "INGEST_LOAD_TARGET_ROOT" not in trigger_env
    assert trigger_env["INGEST_INPUT_BACKEND"]["value"] == "local"
    assert trigger_env["INGEST_INPUT_ROOT"]["value"] == "/nfs-root/autoIngestion"
    trigger_mounts = {item["name"]: item for item in trigger["volumeMounts"]}
    assert trigger_mounts["ingest-input"]["mountPath"] == "/nfs-root/autoIngestion"
    assert trigger_mounts["ingest-input"]["readOnly"] is True
    assert trigger_mounts["ingest-input"]["subPath"] == "autoIngestion"
    trigger_volumes = {
        item["name"]: item for item in deployment[0]["spec"]["template"]["spec"]["volumes"]
    }
    assert trigger_volumes["ingest-input"]["persistentVolumeClaim"]["claimName"] == "llmops-nfs-root"

    sweep = yaml.safe_load((base / "ingest-sweep-cronjob.yaml").read_text(encoding="utf-8"))
    assert sweep["spec"]["suspend"] is True
    sweep_container = sweep["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    sweep_env = {item["name"]: item for item in sweep_container["env"]}
    assert sweep_env["INGEST_LOAD_STAGING_ROOT"]["value"] == "/tmp/ingest-load-staging"
    assert "INGEST_REHEARSAL_ROOT" not in sweep_env
    assert "INGEST_LOAD_TARGET_ROOT" not in sweep_env
    assert sweep_env["INGEST_INPUT_BACKEND"]["value"] == "local"
    assert sweep_env["INGEST_INPUT_ROOT"]["value"] == "/nfs-root/autoIngestion"
    sweep_mounts = {item["name"]: item for item in sweep_container["volumeMounts"]}
    assert sweep_mounts["ingest-input"]["readOnly"] is True
    assert sweep_mounts["ingest-input"]["subPath"] == "autoIngestion"

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
    assert by_name["MINIO_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "jw-ingest-hook-minio"


def test_rendered_job_env_minimal_without_s3(monkeypatch):
    for name in ("INGEST_S3_BUCKET", "MARIADB_HOST", "INGEST_REHEARSAL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="/m.json", namespace="llmops")
    names = [e["name"] for e in body["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert "INGEST_S3_BUCKET" not in names and "MARIADB_USER" in names


def test_rendered_job_passes_load_staging_root(monkeypatch):
    monkeypatch.setenv("INGEST_LOAD_STAGING_ROOT", "/tmp/ingest-load-staging")
    monkeypatch.setenv("INGEST_LOAD_STAGING_DB", "jw_ingest_stage_hook")
    monkeypatch.setenv("INGEST_COMPLETION_WEBHOOK_URL", "https://agent.invalid/ingest")
    monkeypatch.setenv("INGEST_COMPLETION_WEBHOOK_ATTEMPTS", "5")
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="_manifests/m.json", namespace="llmops")
    env = {e["name"]: e for e in body["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["INGEST_LOAD_STAGING_ROOT"]["value"] == "/tmp/ingest-load-staging"
    assert env["INGEST_LOAD_STAGING_DB"]["value"] == "jw_ingest_stage_hook"
    assert env["INGEST_COMPLETION_WEBHOOK_URL"]["value"] == "https://agent.invalid/ingest"
    assert env["INGEST_COMPLETION_WEBHOOK_ATTEMPTS"]["value"] == "5"


def test_rendered_local_job_inherits_backend_root_and_read_only_nfs(monkeypatch):
    monkeypatch.setenv("INGEST_INPUT_BACKEND", "local")
    monkeypatch.setenv("INGEST_INPUT_ROOT", "/nfs-root/autoIngestion")
    monkeypatch.setenv("INGEST_S3_BUCKET", "legacy-bucket-must-not-win")

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/nfs-root/autoIngestion/_manifests/ubist/2026-03/manifest.json",
        namespace="llmops",
    )
    pod_spec = body["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = {item["name"]: item for item in container["env"]}

    assert env["INGEST_INPUT_BACKEND"]["value"] == "local"
    assert env["INGEST_INPUT_ROOT"]["value"] == "/nfs-root/autoIngestion"
    assert "INGEST_S3_BUCKET" not in env
    assert container["volumeMounts"] == [
        {
            "name": "ingest-input",
            "mountPath": "/nfs-root/autoIngestion",
            "subPath": "autoIngestion",
            "readOnly": True,
        }
    ]
    assert pod_spec["volumes"] == [
        {"name": "ingest-input", "persistentVolumeClaim": {"claimName": "llmops-nfs-root"}}
    ]
