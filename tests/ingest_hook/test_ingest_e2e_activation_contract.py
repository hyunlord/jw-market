from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.job_launcher import render_job


PRODUCTION_CATEGORIES = (
    "iqvia_nsa",
    "iqvia_csd_channel",
    "iqvia_csd_keyword",
    "mi_master",
)
TARGET_ENVS = {
    "INGEST_CATEGORY_TARGET_IQVIA_NSA_DB": "jw_mart_d2_stage_20260630_r2",
    "INGEST_CATEGORY_TARGET_CSD_RAW_DB": "jw_brand_activity_raw",
    "INGEST_CATEGORY_TARGET_CSD_STAGE_DB": "jw_brand_activity_stage",
    "INGEST_CATEGORY_TARGET_MI_MASTER_DB": "jw_mart_d2_stage_20260630_r2",
}
SERVING_DB = "jw_mart_d2_stage_20260630_r2"
ACTIVATION_OVERLAYS = (
    "ingest-job-activation-overlay.yaml",
    "ingest-job-activation-test2-overlay.yaml",
)
TRIGGER_PRODUCTION_OVERLAY = (
    "deploy/k8s/ingest-hook/reference/"
    "ingest-trigger-production-overlay.yaml"
)
SEMANTIC_REPLAY_JOB = (
    "deploy/k8s/ingest-hook/reference/semantic-replay-job-template.yaml"
)
COMPLETION_URL = "http://jw-ingest-hook.llmops.svc.cluster.local/ingest/reconcile"


def test_all_market_categories_have_a_production_activation_contract() -> None:
    overlay = yaml.safe_load(
        Path(
            "deploy/k8s/ingest-hook/reference/ingest-job-activation-overlay.yaml"
        ).read_text(encoding="utf-8")
    )
    env = _overlay_env(overlay)

    assert set(PRODUCTION_CATEGORIES) == {
        "iqvia_nsa",
        "iqvia_csd_channel",
        "iqvia_csd_keyword",
        "mi_master",
    }
    for name, value in TARGET_ENVS.items():
        assert env[name]["value"] == value


def test_rendered_production_job_inherits_activation_and_publication_contract(
    monkeypatch,
) -> None:
    image = "registry.example/jw-pipeline@sha256:" + ("b" * 64)
    monkeypatch.setenv("INGEST_LOAD_TARGET_ROOT", "/market-output")
    monkeypatch.setenv("INGEST_LOAD_PRODUCTION_DB", SERVING_DB)
    monkeypatch.setenv("INGEST_MART_PROMOTION_APPROVED", "1")
    monkeypatch.setenv("INGEST_PUBLICATION_EPOCH_TABLE", "mart_publication_epoch")
    monkeypatch.setenv("INGEST_COMPLETION_WEBHOOK_URL", COMPLETION_URL)
    monkeypatch.setenv("BUILD_GIT_SHA", "a" * 40)
    monkeypatch.setenv("INGEST_JOB_IMAGE", image)
    for name, value in TARGET_ENVS.items():
        monkeypatch.setenv(name, value)

    for category in PRODUCTION_CATEGORIES:
        body = render_job(
            category=category,
            manifest_sha="a" * 64,
            manifest_path=f"_manifests/{category}/2026-Q2/manifest.json",
            run_id="contract",
        )
        container = body["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}

        assert env["INGEST_LOAD_TARGET_ROOT"] == "/market-output"
        assert env["INGEST_LOAD_PRODUCTION_DB"] == SERVING_DB
        assert env["INGEST_MART_PROMOTION_APPROVED"] == "1"
        assert env["INGEST_PUBLICATION_EPOCH_TABLE"] == "mart_publication_epoch"
        assert env["INGEST_COMPLETION_WEBHOOK_URL"] == COMPLETION_URL
        assert env["BUILD_GIT_SHA"] == "a" * 40
        assert env["INGEST_JOB_IMAGE"] == image
        for name, value in TARGET_ENVS.items():
            assert env[name] == value


def test_activation_overlays_declare_loader_and_publication_wiring() -> None:
    for overlay_name in ACTIVATION_OVERLAYS:
        overlay = yaml.safe_load(
            Path(f"deploy/k8s/ingest-hook/reference/{overlay_name}").read_text(
                encoding="utf-8"
            )
        )
        container = overlay["spec"]["template"]["spec"]["containers"][0]
        env = _overlay_env(overlay)

        assert container["image"] == config.DEFAULT_JOB_IMAGE
        assert env["INGEST_LOAD_TARGET_ROOT"]["value"] == "/market-output"
        assert env["INGEST_LOAD_PRODUCTION_DB"]["value"]
        assert env["INGEST_PUBLICATION_EPOCH_TABLE"]["value"] == "mart_publication_epoch"
        assert env["INGEST_COMPLETION_WEBHOOK_URL"]["value"] == COMPLETION_URL
        for name in TARGET_ENVS:
            assert env[name]["value"]
        assert "INGEST_LOAD_STAGING_ROOT" not in env
        assert "cache_cause" not in str(overlay)
        assert "cache_deep_analysis" not in str(overlay)


def test_completion_webhook_endpoint_targets_existing_hook_service_and_route() -> None:
    docs = list(
        yaml.safe_load_all(
            Path("deploy/k8s/ingest-hook/ingest-trigger-deployment.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    service = next(doc for doc in docs if doc["kind"] == "Service")
    app_source = Path("pipeline/scripts/ingest_hook/app.py").read_text(encoding="utf-8")

    assert service["kind"] == "Service"
    assert service["metadata"]["name"] == "jw-ingest-hook"
    assert service["spec"]["ports"][0]["port"] == 8080
    assert '@app.post("/ingest/reconcile")' in app_source


def test_trigger_production_overlay_is_not_shadow_or_staging() -> None:
    overlay = yaml.safe_load(
        Path(TRIGGER_PRODUCTION_OVERLAY).read_text(encoding="utf-8")
    )
    container = overlay["spec"]["template"]["spec"]["containers"][0]
    assert container["env"][0] == {"$patch": "replace"}
    env = _overlay_env(overlay)

    assert env["INGEST_LOAD_TARGET_ROOT"]["value"] == "/market-output"
    assert env["INGEST_LOAD_PRODUCTION_DB"]["value"] == SERVING_DB
    assert env["INGEST_MART_PROMOTION_APPROVED"]["value"] == "1"
    assert env["INGEST_CATEGORY_ACTIVATION_APPROVED"]["value"] == "1"
    assert env["INGEST_PUBLICATION_PROVENANCE_TABLE"]["value"] == (
        "mart_publication_provenance"
    )
    assert env["BUILD_GIT_SHA"]["value"] == (
        "ca49945bc15df260f43134c6026a98fd5a5f47c4"
    )
    assert env["INGEST_JOB_IMAGE"]["value"] == config.DEFAULT_JOB_IMAGE
    assert "INGEST_LOAD_STAGING_ROOT" not in env
    assert "INGEST_LOAD_SHADOW_ROOT" not in env
    assert "INGEST_SHADOW_LEDGER_SQLITE" not in env
    assert "semantic" not in str(overlay).casefold()


def test_semantic_replay_runs_in_a_separate_resource_bounded_job() -> None:
    body = yaml.safe_load(Path(SEMANTIC_REPLAY_JOB).read_text(encoding="utf-8"))
    container = body["spec"]["template"]["spec"]["containers"][0]

    assert body["kind"] == "Job"
    assert body["spec"]["backoffLimit"] == 0
    assert container["command"] == [
        "python",
        "-m",
        "pipeline.scripts.ingest_hook.semantic_replay",
    ]
    assert "--memory-limit" in container["args"]
    assert "--temp-directory" in container["args"]
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert body["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def _overlay_env(overlay: dict) -> dict[str, dict]:
    container = overlay["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item for item in container["env"] if "name" in item}
