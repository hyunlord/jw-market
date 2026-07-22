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
    assert spec["activeDeadlineSeconds"] == 43200
    assert spec["ttlSecondsAfterFinished"] == 86400
    assert spec["template"]["spec"]["restartPolicy"] == "Never"
    assert container["resources"] == {
        "requests": {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "20Gi"},
        "limits": {"cpu": "4", "memory": "16Gi", "ephemeral-storage": "50Gi"},
    }
    assert container["image"].endswith(":REPLACE_WITH_PINNED_DIGEST")


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


def test_full_rehearsal_job_pins_the_source_snapshot_and_evidence_run() -> None:
    job = _job()
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}
    mounts = {entry["name"]: entry for entry in container["volumeMounts"]}
    script = container["args"][0]

    assert env["R1_SOURCE_SUBPATH"] == "REPLACE_WITH_PINNED_SOURCE_SUBPATH"
    assert env["R1_RUN_ID"] == "REPLACE_WITH_UNIQUE_RUN_ID"
    assert mounts["r1-source"]["subPathExpr"] == "$(R1_SOURCE_SUBPATH)"
    assert mounts["r1-source"]["readOnly"] is True
    assert 'EVIDENCE_DIR="/work/evidence/${R1_RUN_ID}"' in script
    assert 'tee "${EVIDENCE_DIR}/rehearse.log"' in script
