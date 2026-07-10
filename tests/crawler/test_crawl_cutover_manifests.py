from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_IMAGE_DIGEST = "64bb2b9f2ad213a06392d5caf9ea4191615d265ecdcfb52b64bba59ae9171268"


def _manifest(name: str) -> str:
    return (REPO_ROOT / "deploy" / "k8s" / "crawler" / name).read_text(encoding="utf-8")


def test_tier1_manifest_uses_rev5674_marker_and_redesigned_path() -> None:
    manifest = _manifest("crawl-tier1-cronjob.yaml")

    assert f"@sha256:{CANONICAL_IMAGE_DIGEST}" in manifest
    assert "--processed-by workflow_196_rev5674" in manifest
    assert "--months 1" in manifest
    assert "PRESEED_URL_COUNT" in manifest
    assert "CANDIDATE_GATE" in manifest
    assert "suspend: true" in manifest


def test_tier2_manifest_pins_ga_workflow_and_stays_suspended() -> None:
    manifest = _manifest("crawl-tier2-cronjob.yaml")

    assert f"@sha256:{CANONICAL_IMAGE_DIGEST}" in manifest
    assert "name: WF337_URL" in manifest
    assert "http://workflow-337.llmops.svc.cluster.local:8080/run/v2" in manifest
    assert "suspend: true" in manifest
