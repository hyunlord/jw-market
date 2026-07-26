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


def test_hira_worker_is_separate_recreate_deployment_with_rwo_state() -> None:
    documents = _documents()
    deployment = next(
        document for document in documents if document["kind"] == "Deployment"
    )
    claim = next(
        document
        for document in documents
        if document["kind"] == "PersistentVolumeClaim"
    )

    assert deployment["metadata"]["name"] == "jw-hira-benefit-worker"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]


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
