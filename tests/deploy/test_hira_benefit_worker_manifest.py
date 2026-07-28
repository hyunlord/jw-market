from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy/k8s/hira-benefit/worker.yaml"
DOCKERFILE = ROOT / "deploy/docker/hira-benefit-worker.Dockerfile"


def _documents() -> list[dict[str, object]]:
    return [
        document
        for document in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
        if document is not None
    ]


def test_hira_worker_is_separate_recreate_deployment_with_api01_rwo_state() -> None:
    documents = _documents()
    deployment = next(
        document for document in documents if document["kind"] == "Deployment"
    )
    claims = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "PersistentVolumeClaim"
    }
    pod_spec = deployment["spec"]["template"]["spec"]

    assert deployment["metadata"]["name"] == "jw-hira-benefit-worker"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert set(claims) == {
        "jw-hira-benefit-state",
        "jw-hira-benefit-state-api01",
    }
    assert claims["jw-hira-benefit-state-api01"]["spec"]["accessModes"] == [
        "ReadWriteOnce"
    ]
    assert (
        claims["jw-hira-benefit-state-api01"]["spec"]["storageClassName"]
        == "standard-rwo"
    )
    assert pod_spec["nodeSelector"] == {
        "cloud.google.com/gke-nodepool": "knp-jw-agn-dev-genos-api-01"
    }
    assert pod_spec["volumes"] == [
        {
            "name": "state",
            "persistentVolumeClaim": {
                "claimName": "jw-hira-benefit-state-api01"
            },
        }
    ]


def test_hira_worker_runtime_contract_is_baked_into_manifest() -> None:
    deployment = next(
        document for document in _documents() if document["kind"] == "Deployment"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item for item in container["env"]}

    assert container["command"] == [
        "python",
        "-m",
        "pipeline.scripts.crawler.hira_benefit.temporal_worker",
    ]
    assert environment["HIRA_TEMPORAL_TASK_QUEUE"]["value"] == (
        "jw-market-hira-benefit-v1"
    )
    assert environment["HIRA_USER_AGENT"]["value"] == (
        "JW-Healthcare-DataCollector/1.0 (+mailto:kwanhyeon.park@jwhealthcare.com)"
    )
    assert environment["APP_VERSION"]["value"] == "${APP_VERSION}"
    assert container["image"] == "${IMAGE_DIGEST}"


def test_hira_worker_image_installs_temporal_without_news_entrypoint() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")

    assert '"temporalio==1.20.0"' in source
    assert "hira_benefit.temporal_worker" in source
    assert "crawl_chain" not in source
