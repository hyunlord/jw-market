from pathlib import Path

import yaml


MANIFEST = Path("deploy/k8s/orchestrator/pipeline-orchestrator-full-rehearsal-job.yaml")


def _job() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_full_rehearsal_job_is_protected_from_cluster_scale_down() -> None:
    job = _job()

    annotations = job["spec"]["template"]["metadata"]["annotations"]
    assert annotations["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"


def test_full_rehearsal_job_is_fail_closed_and_resource_bounded() -> None:
    job = _job()
    spec = job["spec"]
    container = spec["template"]["spec"]["containers"][0]

    assert spec["suspend"] is True
    assert spec["backoffLimit"] == 0
    assert spec["activeDeadlineSeconds"] == 21600
    assert spec["template"]["spec"]["restartPolicy"] == "Never"
    assert container["resources"] == {
        "requests": {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "20Gi"},
        "limits": {"cpu": "4", "memory": "16Gi", "ephemeral-storage": "50Gi"},
    }
    assert "@sha256:" in container["image"]


def test_full_rehearsal_job_requires_an_isolated_target_database() -> None:
    job = _job()
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}

    assert env["R1_TARGET_DB"] == "REPLACE_WITH_NEW_ISOLATED_DB"
    assert env["R1_CACHE_DB"] == "REPLACE_WITH_NEW_ISOLATED_CACHE_DB"
    assert env["R1_SOURCE_DB"] == "jw_mart_d2_stage_20260630_r2"
    assert "REPLACE_WITH_NEW_ISOLATED_DB" in container["args"][0]
    assert "jw_mart_d2_stage_20260630_r2" not in {
        env["R1_TARGET_DB"],
        env["R1_CACHE_DB"],
    }


def test_full_rehearsal_job_provisions_only_the_isolated_databases() -> None:
    job = _job()
    pod_spec = job["spec"]["template"]["spec"]
    assert len(pod_spec["initContainers"]) == 1

    provisioner = pod_spec["initContainers"][0]
    rehearsal = pod_spec["containers"][0]
    provisioner_env = {entry["name"]: entry for entry in provisioner["env"]}
    rehearsal_env = {entry["name"]: entry for entry in rehearsal["env"]}

    assert provisioner["image"] == rehearsal["image"]
    assert "provision-full-rehearsal" in provisioner["args"][0]
    assert provisioner_env["R1_TARGET_DB"]["value"] == "REPLACE_WITH_NEW_ISOLATED_DB"
    assert provisioner_env["R1_CACHE_DB"]["value"] == "REPLACE_WITH_NEW_ISOLATED_CACHE_DB"
    assert provisioner_env["R1_WRITER_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-mart-d2-writer",
        "key": "username",
    }
    assert provisioner_env["MARIADB_ROOT_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "galera-mariadb-galera",
        "key": "mariadb-root-password",
    }
    assert "MARIADB_ROOT_PASSWORD" not in rehearsal_env


def test_full_rehearsal_main_container_remains_writer_only() -> None:
    job = _job()
    rehearsal = job["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry for entry in rehearsal["env"]}

    assert env["MARIADB_USER"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-mart-d2-writer",
        "key": "username",
    }
    assert env["MARIADB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "jw-mart-d2-writer",
        "key": "password",
    }
