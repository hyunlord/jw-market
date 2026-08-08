"""Job rendering/submission: image pin, G3-first command, labels, RBAC scope."""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.job_launcher import (
    render_agent_refresh_job,
    render_job,
    render_publish_job,
    submit_job,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = "f" * 64
EXPECTED_API_NODE_AFFINITY = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "cloud.google.com/gke-nodepool",
                            "operator": "In",
                            "values": ["knp-jw-agn-dev-genos-api-01"],
                        }
                    ]
                }
            ]
        }
    }
}
EXPECTED_SCALE_DOWN_ANNOTATIONS = {
    "cluster-autoscaler.kubernetes.io/safe-to-evict": "false"
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
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["INGEST_IMAGE_DIGEST"] == f"sha256:{config.DEFAULT_JOB_IMAGE.rsplit('sha256:', 1)[1]}"


def test_rendered_job_rejects_unpinned_image(monkeypatch):
    monkeypatch.setenv(config.ENV_JOB_IMAGE, "registry.example/jw-pipeline:latest")

    with pytest.raises(RuntimeError, match="pinned by digest"):
        render_job(
            category="iqvia_nsa",
            manifest_sha=SHA,
            manifest_path="/data/nsa.manifest.json",
            namespace="llmops",
        )


def test_agent_refresh_job_is_a_separate_profile_and_failure_domain():
    body = render_agent_refresh_job(
        epoch="2026-05",
        category="ubist",
        manifest_sha=SHA,
        ingest_run_id="run-1",
        namespace="llmops",
    )

    container = body["spec"]["template"]["spec"]["containers"][0]
    assert body["metadata"]["labels"]["app"] == "jw-agent-refresh"
    assert body["metadata"]["labels"]["jw-ingest/parent-run-id"] == "run-1"
    assert container["command"] == [
        "python",
        "-m",
        "pipeline.scripts.ingest_hook.agent_refresh_runner",
        "--epoch",
        "2026-05",
        "--category",
        "ubist",
        "--manifest-sha",
        SHA,
        "--ingest-run-id",
        "run-1",
    ]
    assert container["image"] == config.DEFAULT_JOB_IMAGE


def test_rendered_job_requires_api_node_pool_for_nfs_mounts():
    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
    )

    pod_spec = body["spec"]["template"]["spec"]
    assert "nodeSelector" not in pod_spec
    assert pod_spec["affinity"] == EXPECTED_API_NODE_AFFINITY


def test_rendered_job_is_protected_from_cluster_scale_down():
    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
    )

    assert body["spec"]["template"]["metadata"]["annotations"] == (
        EXPECTED_SCALE_DOWN_ANNOTATIONS
    )


def test_reference_job_requires_same_api_node_pool():
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
    container = pod_spec["containers"][0]
    assert "nodeSelector" not in pod_spec
    assert pod_spec["affinity"] == EXPECTED_API_NODE_AFFINITY
    assert template["spec"]["backoffLimit"] == 0
    assert container["resources"] == {
        "requests": {"cpu": "2", "memory": "12Gi"},
        "limits": {"cpu": "2", "memory": "12Gi"},
    }


def test_reference_job_is_protected_from_cluster_scale_down():
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

    assert template["spec"]["template"]["metadata"]["annotations"] == (
        EXPECTED_SCALE_DOWN_ANNOTATIONS
    )


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


def test_rendered_retries_share_manifest_scoped_durable_stage_checkpoint():
    first = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
        run_id="retry-one",
    )
    second = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
        run_id="retry-two",
    )

    def state_path(body: dict) -> str:
        container = body["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        return env["JW_PIPELINE_STATE_FILE"]

    expected = f"/market-output/ingest-checkpoints/ubist/{SHA}/orchestrator-state.json"
    assert state_path(first) == expected
    assert state_path(second) == expected


def test_rendered_build_job_does_not_poll_for_publish_approval(monkeypatch):
    monkeypatch.setenv("INGEST_REQUIRE_EXACT_PUBLISH_APPROVAL", "1")
    run_id = "run-a4"

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="/data/m.json",
        namespace="llmops",
        run_id=run_id,
    )

    container = body["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["INGEST_REQUIRE_EXACT_PUBLISH_APPROVAL"] == "1"
    assert "INGEST_PUBLISH_APPROVAL_FILE" not in env
    assert body["spec"]["activeDeadlineSeconds"] == 28800


def test_rendered_publish_job_is_separate_immutable_lifecycle(monkeypatch):
    monkeypatch.setenv("INGEST_REQUIRE_EXACT_PUBLISH_APPROVAL", "1")

    body = render_publish_job(
        epoch="2026-05",
        category="ubist",
        manifest_sha=SHA,
        build_run_id="build-run",
        publish_run_id="publish-run",
        namespace="llmops",
    )

    container = body["spec"]["template"]["spec"]["containers"][0]
    assert body["metadata"]["name"] == f"jw-ingest-publish-ubist-{SHA[:8]}-publish-run"
    assert body["metadata"]["labels"]["app"] == "jw-ingest-publish"
    assert body["metadata"]["labels"]["jw-ingest/parent-run-id"] == "build-run"
    assert body["spec"]["activeDeadlineSeconds"] == 7200
    assert container["command"] == [
        "python",
        "-m",
        "pipeline.scripts.ingest_hook.publish_runner",
        "--epoch",
        "2026-05",
        "--category",
        "ubist",
        "--manifest-sha",
        SHA,
        "--build-run-id",
        "build-run",
        "--publish-run-id",
        "publish-run",
    ]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert "INGEST_PUBLISH_APPROVAL_FILE" not in env


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
    assert trigger_env["AGENT3_DB_NAME"]["value"] == "jw_mart_d2_stage_20260630_r2"
    assert trigger_env["AGENT3_WORKFLOW_REV"]["value"] == "5692"
    assert trigger_env["AGENT3_EXPECTED_WORKFLOW_REV"]["value"] == "5692"
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
    assert role["rules"] == [
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["create", "get", "list", "delete"],
        }
    ]


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
    monkeypatch.setenv("MARIADB_PORT", "3307")
    monkeypatch.setenv("MARIADB_DATABASE", "jw_mart_serving")
    monkeypatch.setenv("INGEST_S3_BUCKET", "jw-market-raw")
    monkeypatch.setenv("INGEST_REHEARSAL_ROOT", "/tmp/ingest-rehearsal")
    monkeypatch.setenv("AGENT3_DB_NAME", "agent3-live")
    monkeypatch.setenv("AGENT3_WORKFLOW_REV", "5692")
    monkeypatch.setenv("AGENT3_EXPECTED_WORKFLOW_REV", "5692")
    monkeypatch.setenv("APP_VERSION", "a" * 40)
    monkeypatch.setenv(
        "INGEST_JOB_IMAGE", "registry.example/pipeline@sha256:" + ("b" * 64)
    )
    body = render_job(category="ubist", manifest_sha=SHA, manifest_path="_manifests/m.json", namespace="llmops")
    env = body["spec"]["template"]["spec"]["containers"][0]["env"]
    by_name = {e["name"]: e for e in env}
    assert by_name["MARIADB_HOST"]["value"] == "db.example"
    assert by_name["DB_HOST"]["value"] == "db.example"
    assert by_name["DB_PORT"]["value"] == "3307"
    assert by_name["DB_NAME"]["value"] == "jw_mart_serving"
    assert (
        by_name["DB_USER"]["valueFrom"]["secretKeyRef"]
        == {"name": "jw-mart-d2-writer", "key": "username"}
    )
    assert (
        by_name["DB_ROOT_PASSWORD"]["valueFrom"]["secretKeyRef"]
        == {"name": "jw-mart-d2-writer", "key": "password"}
    )
    assert (
        by_name["DB_PASSWORD"]["valueFrom"]["secretKeyRef"]
        == {"name": "jw-mart-d2-writer", "key": "password"}
    )
    assert by_name["INGEST_REHEARSAL_ROOT"]["value"] == "/tmp/ingest-rehearsal"
    assert by_name["AGENT3_DB_NAME"]["value"] == "agent3-live"
    assert by_name["AGENT3_DB_HOST"]["value"] == "db.example"
    assert by_name["AGENT3_DB_PORT"]["value"] == "3307"
    assert (
        by_name["AGENT3_DB_USER"]["valueFrom"]["secretKeyRef"]
        == {"name": "jw-mart-d2-writer", "key": "username"}
    )
    assert (
        by_name["AGENT3_DB_PASSWORD"]["valueFrom"]["secretKeyRef"]
        == {"name": "jw-mart-d2-writer", "key": "password"}
    )
    assert by_name["AGENT3_WORKFLOW_REV"]["value"] == "5692"
    assert by_name["AGENT3_EXPECTED_WORKFLOW_REV"]["value"] == "5692"
    assert by_name["APP_VERSION"]["value"] == "a" * 40
    assert by_name["INGEST_JOB_IMAGE"]["value"].endswith("@sha256:" + ("b" * 64))
    assert by_name["NPY_DISABLE_CPU_FEATURES"]["value"] == "X86_V3,X86_V4"
    assert by_name["OPENBLAS_CORETYPE"]["value"] == "Nehalem"
    assert by_name["OMP_NUM_THREADS"]["value"] == "1"
    assert by_name["OPENBLAS_NUM_THREADS"]["value"] == "1"
    assert by_name["MKL_NUM_THREADS"]["value"] == "1"
    assert by_name["NUMEXPR_NUM_THREADS"]["value"] == "1"
    assert by_name["MARIADB_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "jw-mart-d2-writer"
    assert by_name["INGEST_S3_BUCKET"]["valueFrom"]["secretKeyRef"]["key"] == "MINIO_MARKET_BUCKET"
    assert by_name["MINIO_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "jw-ingest-hook-minio"


def test_csd_channel_job_alone_receives_dedicated_activation_credentials(monkeypatch):
    monkeypatch.setenv("MARIADB_HOST", "db.example")
    csd = render_job(
        category="iqvia_csd_channel",
        manifest_sha=SHA,
        manifest_path="_manifests/csd.json",
        namespace="llmops",
    )
    ubist = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="_manifests/ubist.json",
        namespace="llmops",
    )
    csd_env = {
        item["name"]: item
        for item in csd["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    ubist_names = {
        item["name"]
        for item in ubist["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert csd_env["CSD_CHANNEL_DB_HOST"]["value"] == "db.example"
    assert csd_env["CSD_CHANNEL_DB_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-csd-channel-activator",
        "key": "username",
    }
    assert csd_env["CSD_CHANNEL_DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-csd-channel-activator",
        "key": "password",
    }
    assert "CSD_CHANNEL_DB_USER" not in ubist_names
    assert "CSD_CHANNEL_DB_PASSWORD" not in ubist_names


def test_csd_keyword_job_receives_dedicated_activation_credentials(monkeypatch):
    keyword = render_job(
        category="iqvia_csd_keyword",
        manifest_sha=SHA,
        manifest_path="_manifests/keyword.json",
        namespace="llmops",
    )
    by_name = {
        item["name"]: item
        for item in keyword["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert by_name["CSD_CHANNEL_DB_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-csd-channel-activator",
        "key": "username",
    }
    assert by_name["CSD_CHANNEL_DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-csd-channel-activator",
        "key": "password",
    }


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


def test_rendered_job_passes_full_scan_and_automatic_publish_contract(monkeypatch):
    policies = (
        '{"ubist":{"root":"/nfs-root/autoIngestion/ubist",'
        '"period_unit":"month","excluded_relative_roots":[]}}'
    )
    monkeypatch.setenv("INGEST_FULL_SCAN_ENABLED", "1")
    monkeypatch.setenv("INGEST_SOURCE_SCAN_POLICIES_JSON", policies)
    monkeypatch.setenv(
        "INGEST_AUTOMATIC_PUBLISH_WEBHOOK_URL",
        "http://jw-ingest-hook.llmops.svc.cluster.local:8080/ingest/publish/automatic",
    )

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="_manifests/m.json",
        namespace="llmops",
    )

    env = {
        item["name"]: item["value"]
        for item in body["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["INGEST_FULL_SCAN_ENABLED"] == "1"
    assert env["INGEST_SOURCE_SCAN_POLICIES_JSON"] == policies
    assert env["INGEST_AUTOMATIC_PUBLISH_WEBHOOK_URL"].endswith(
        "/ingest/publish/automatic"
    )


def test_rendered_job_passes_e2e_commissioning_override(monkeypatch):
    monkeypatch.setenv("INGEST_E2E_COMMISSIONING", "1")

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="_manifests/m.json",
        namespace="llmops",
    )

    env = {
        item["name"]: item["value"]
        for item in body["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["INGEST_E2E_COMMISSIONING"] == "1"


def test_rendered_job_passes_production_activation_categories(monkeypatch):
    monkeypatch.setenv(
        "INGEST_PRODUCTION_LOAD_CATEGORIES",
        "iqvia_csd_channel,iqvia_csd_keyword",
    )

    body = render_job(
        category="iqvia_csd_channel",
        manifest_sha=SHA,
        manifest_path="_manifests/m.json",
        namespace="llmops",
    )

    env = {
        item["name"]: item["value"]
        for item in body["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["INGEST_PRODUCTION_LOAD_CATEGORIES"] == (
        "iqvia_csd_channel,iqvia_csd_keyword"
    )


def test_rendered_job_passes_production_catalog_inputs(monkeypatch):
    monkeypatch.setenv("JW_MARKET_CATALOG_ROOT", "/market-output/catalog")
    monkeypatch.setenv(
        "INGEST_CATALOG_IQVIA_NSA_DIR",
        "/market-output/catalog-inputs/iqvia_nsa",
    )

    body = render_job(
        category="ubist",
        manifest_sha=SHA,
        manifest_path="_manifests/m.json",
        namespace="llmops",
    )

    env = {
        item["name"]: item
        for item in body["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["JW_MARKET_CATALOG_ROOT"]["value"] == "/market-output/catalog"
    assert (
        env["INGEST_CATALOG_IQVIA_NSA_DIR"]["value"]
        == "/market-output/catalog-inputs/iqvia_nsa"
    )


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
        "requests": {"cpu": "2", "memory": "8Gi"},
        "limits": {"cpu": "4", "memory": "16Gi"},
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
        "requests": {"cpu": "2", "memory": "12Gi"},
        "limits": {"cpu": "2", "memory": "12Gi"},
    }
    assert env["INGEST_MART_SOURCE_DB"]["value"] == "jw_mart"
    assert env["INGEST_MART_TARGET_DB"]["value"] == "jw_mart"
    assert env["INGEST_MART_BUILD_PREFIX"]["value"] == "jw_mart_ingest"


def test_rendered_job_rejects_staging_and_target_roots(monkeypatch):
    monkeypatch.setenv("INGEST_LOAD_STAGING_ROOT", "/tmp/staging")
    monkeypatch.setenv("INGEST_LOAD_TARGET_ROOT", "/market-output")

    with pytest.raises(RuntimeError, match="mutually exclusive"):
        render_job(category="ubist", manifest_sha=SHA, manifest_path="/m.json")
