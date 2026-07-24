"""Job rendering/submission: image pin, G3-first command, labels, RBAC scope."""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.job_launcher import render_job, submit_job

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = "f" * 64
EXPECTED_API_NODE_AFFINITY = {
    "nodeAffinity": {
        "preferredDuringSchedulingIgnoredDuringExecution": [
            {
                "weight": 100,
                "preference": {
                    "matchExpressions": [
                        {
                            "key": "cloud.google.com/gke-nodepool",
                            "operator": "In",
                            "values": ["knp-jw-agn-dev-genos-api-01"],
                        }
                    ]
                },
            }
        ]
    }
}


def test_rendered_job_pins_orchestrator_image_and_runner():
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="/data/m.json", namespace="llmops")
    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == config.DEFAULT_JOB_IMAGE
    assert container["command"][:4] == [
        "python", "-m", "pipeline.scripts.ingest_hook.stage_log_runner", "--manifest"
    ]
    assert body["metadata"]["name"] == f"jw-ingest-ubist-{SHA[:8]}"
    assert body["spec"]["backoffLimit"] == 0
    assert body["metadata"]["labels"]["jw-ingest/category"] == "ubist"


def test_rendered_job_prefers_api_node_pool_without_forcing_scheduling():
    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
    )

    pod_spec = body["spec"]["template"]["spec"]
    assert "nodeSelector" not in pod_spec
    assert pod_spec["affinity"] == EXPECTED_API_NODE_AFFINITY


def test_reference_job_prefers_same_api_node_pool():
    template = yaml.safe_load(
        (
            REPO_ROOT
            / "deploy"
            / "k8s"
            / "ingest-hook"
            / "reference"
            / "ingest-job-template.yaml"
        ).read_text(encoding="utf-8")
    )

    pod_spec = template["spec"]["template"]["spec"]
    assert "nodeSelector" not in pod_spec
    assert pod_spec["affinity"] == EXPECTED_API_NODE_AFFINITY


def test_rendered_retry_passes_one_run_id_to_job_and_durable_log(monkeypatch):
    monkeypatch.setenv("INGEST_LOAD_STAGING_ROOT", "/tmp/ingest-load-staging")
    run_id = "20260723112233445566"

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
        run_id=run_id,
    )

    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == [
        "python",
        "-m",
        "pipeline.scripts.ingest_hook.stage_log_runner",
        "--manifest",
        "/data/m.json",
        "--run-id",
        run_id,
        "--job-name",
        body["metadata"]["name"],
    ]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["INGEST_LOG_ROOT"] == "/market-output/ingest-logs"
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    assert mounts["market-output"]["mountPath"] == "/market-output"
    assert mounts["market-output"]["readOnly"] is False


def test_submit_uses_injected_transport(fake_transport):
    name = submit_job(
        category="iqvia", manifest_sha=SHA, manifest_path="/data/m.json",
        transport=fake_transport, namespace="llmops",
    )
    assert name == f"jw-ingest-iqvia-{SHA[:8]}"
    (url_path, body), = fake_transport.submitted
    assert url_path == "/apis/batch/v1/namespaces/llmops/jobs"
    assert body["metadata"]["name"] == name


def test_submit_scopes_job_name_to_retry_run_id(fake_transport):
    first = submit_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        run_id="20260723032031000001",
        transport=fake_transport,
        namespace="llmops",
    )
    second = submit_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        run_id="20260723034015000002",
        transport=fake_transport,
        namespace="llmops",
    )

    assert first == f"jw-ingest-ubist-{SHA[:8]}-20260723032031000001"
    assert second == f"jw-ingest-ubist-{SHA[:8]}-20260723034015000002"
    assert first != second


def test_rendered_job_sanitizes_category_for_kubernetes_name():
    body = render_job(
        category="iqvia_nsa",
        manifest_sha=SHA,
        manifest_path="/data/nsa.manifest.json",
        namespace="llmops",
    )

    assert body["metadata"]["name"] == f"jw-ingest-iqvia-nsa-{SHA[:8]}"
    assert body["metadata"]["labels"]["jw-ingest/category"] == "iqvia_nsa"


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
    assert sweep["spec"]["schedule"] == "*/5 * * * *"
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


def test_reference_jobs_separate_staging_from_activation_contracts():
    reference = REPO_ROOT / "deploy" / "k8s" / "ingest-hook" / "reference"
    staging = yaml.safe_load((reference / "ingest-job-template.yaml").read_text(encoding="utf-8"))
    activation = yaml.safe_load(
        (reference / "ingest-job-activation-overlay.yaml").read_text(encoding="utf-8")
    )

    staging_spec = staging["spec"]["template"]["spec"]
    staging_container = staging_spec["containers"][0]
    staging_env = {item["name"] for item in staging_container["env"]}
    assert "INGEST_LOAD_STAGING_ROOT" in staging_env
    assert "INGEST_LOAD_TARGET_ROOT" not in staging_env
    assert "market-output" in {item["name"] for item in staging_container["volumeMounts"]}
    assert "market-output" in {item["name"] for item in staging_spec["volumes"]}

    activation_spec = activation["spec"]["template"]["spec"]
    activation_container = activation_spec["containers"][0]
    activation_env = {item["name"] for item in activation_container["env"]}
    assert "INGEST_LOAD_STAGING_ROOT" not in activation_env
    assert "INGEST_LOAD_TARGET_ROOT" in activation_env
    assert "INGEST_MART_PROMOTION_APPROVED" in activation_env
    assert "market-output" in {item["name"] for item in activation_container["volumeMounts"]}


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


def test_rendered_shadow_job_passes_isolated_catalog_root(monkeypatch):
    monkeypatch.setenv("INGEST_SHADOW_CATALOG_ROOT", "/market-output/shadow/catalog")

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="_manifests/m.json",
        namespace="llmops",
    )

    env = {e["name"]: e for e in body["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["INGEST_SHADOW_CATALOG_ROOT"]["value"] == "/market-output/shadow/catalog"


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
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    assert mounts["ingest-input"] == {
        "name": "ingest-input",
        "mountPath": "/nfs-root/autoIngestion",
        "subPath": "autoIngestion",
        "readOnly": True,
    }
    assert mounts["market-output"] == {
        "name": "market-output",
        "mountPath": "/market-output",
        "readOnly": False,
    }
    volumes = {item["name"]: item for item in pod_spec["volumes"]}
    assert volumes["ingest-input"] == {
        "name": "ingest-input",
        "persistentVolumeClaim": {"claimName": "llmops-nfs-root"},
    }
    assert volumes["market-output"] == {
        "name": "market-output",
        "persistentVolumeClaim": {"claimName": "llmops-market-output"},
    }


def test_rendered_production_job_mounts_dedicated_output_pvc_read_write(monkeypatch):
    monkeypatch.setenv("INGEST_INPUT_BACKEND", "local")
    monkeypatch.setenv("INGEST_INPUT_ROOT", "/nfs-root/autoIngestion")
    monkeypatch.delenv("INGEST_LOAD_STAGING_ROOT", raising=False)
    monkeypatch.setenv("INGEST_LOAD_TARGET_ROOT", "/market-output")

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/nfs-root/autoIngestion/_manifests/ubist/2026-03/manifest.json",
        namespace="llmops",
    )

    pod_spec = body["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    volumes = {item["name"]: item for item in pod_spec["volumes"]}
    assert mounts["market-output"] == {
        "name": "market-output",
        "mountPath": "/market-output",
        "readOnly": False,
    }
    assert volumes["market-output"]["persistentVolumeClaim"]["claimName"] == "llmops-market-output"


def test_rendered_shadow_job_mounts_output_without_production_unlock(monkeypatch):
    monkeypatch.setenv("INGEST_INPUT_BACKEND", "local")
    monkeypatch.setenv("INGEST_INPUT_ROOT", "/nfs-root/autoIngestion")
    monkeypatch.delenv("INGEST_LOAD_STAGING_ROOT", raising=False)
    monkeypatch.delenv("INGEST_LOAD_TARGET_ROOT", raising=False)
    monkeypatch.delenv("INGEST_MART_PROMOTION_APPROVED", raising=False)
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.setenv("INGEST_SHADOW_TARGET_DB", "jw_mart_ingest_shadow_demo")
    monkeypatch.setenv("INGEST_SHADOW_LEDGER_SQLITE", "/market-output/shadow/ledger.sqlite")
    monkeypatch.setenv("INGEST_SHADOW_FAILURE_AT", "sigma_parts_whole")
    monkeypatch.setenv("INGEST_SHADOW_CRASH_AT", "after_mart_publish")

    body = render_job(
        category="ubist", manifest_sha=SHA, manifest_path="/m.json", namespace="llmops"
    )

    pod_spec = body["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    assert env["INGEST_LOAD_SHADOW_ROOT"]["value"] == "/market-output/shadow"
    assert env["INGEST_SHADOW_TARGET_DB"]["value"] == "jw_mart_ingest_shadow_demo"
    assert env["INGEST_SHADOW_FAILURE_AT"]["value"] == "sigma_parts_whole"
    assert env["INGEST_SHADOW_CRASH_AT"]["value"] == "after_mart_publish"
    assert "INGEST_LOAD_TARGET_ROOT" not in env
    assert "INGEST_MART_PROMOTION_APPROVED" not in env
    assert mounts["market-output"]["mountPath"] == "/market-output"
    assert mounts["market-output"]["readOnly"] is False
    assert container["resources"] == {
        "requests": {"cpu": "2", "memory": "24Gi"},
        "limits": {"cpu": "4", "memory": "32Gi"},
    }


def test_rendered_production_job_passes_mart_activation_contract(monkeypatch):
    monkeypatch.delenv("INGEST_LOAD_STAGING_ROOT", raising=False)
    monkeypatch.setenv("INGEST_LOAD_TARGET_ROOT", "/market-output")
    monkeypatch.setenv("INGEST_MART_PROMOTION_APPROVED", "1")
    monkeypatch.setenv("INGEST_MART_SOURCE_DB", "jw_mart")
    monkeypatch.setenv("INGEST_MART_TARGET_DB", "jw_mart")
    monkeypatch.setenv("INGEST_MART_BUILD_PREFIX", "jw_mart_ingest")

    body = render_job(
        category="ubist", manifest_sha=SHA, manifest_path="/m.json", namespace="llmops"
    )
    env = {
        item["name"]: item
        for item in body["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INGEST_MART_PROMOTION_APPROVED"]["value"] == "1"
    assert body["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    }
    assert env["INGEST_MART_SOURCE_DB"]["value"] == "jw_mart"
    assert env["INGEST_MART_TARGET_DB"]["value"] == "jw_mart"
    assert env["INGEST_MART_BUILD_PREFIX"]["value"] == "jw_mart_ingest"


def test_rendered_job_rejects_staging_and_target_roots(monkeypatch):
    monkeypatch.setenv("INGEST_LOAD_STAGING_ROOT", "/tmp/staging")
    monkeypatch.setenv("INGEST_LOAD_TARGET_ROOT", "/market-output")

    with pytest.raises(RuntimeError, match="mutually exclusive"):
        render_job(category="ubist", manifest_sha=SHA, manifest_path="/m.json")
