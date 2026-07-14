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


def test_tier2_manifest_pins_ga_workflow_and_stays_active() -> None:
    manifest = _manifest("crawl-tier2-cronjob.yaml")

    assert f"@sha256:{CANONICAL_IMAGE_DIGEST}" in manifest
    assert "name: WF337_URL" in manifest
    assert "http://workflow-337.llmops.svc.cluster.local:8080/run/v2" in manifest
    assert "append-live" in manifest
    assert "--target-processor tier2_llm_v2_rev5671" in manifest
    assert "--daily-call-limit 60" in manifest
    assert "--max-cost-krw 203.40" in manifest
    assert "python /opt/tier2/tier2_full_scoring_runner.py append-live" in manifest
    assert "python /opt/tier2/tier2_full_scoring_runner.py sync-events-raw" in manifest
    assert manifest.index("sync-events-raw") < manifest.index("append-live")
    assert "name: tier2-llm-runner-rev5671" in manifest
    assert "suspend: false" in manifest


def test_tier2_apply_script_generates_configmap_from_canonical_runner() -> None:
    script = _manifest("apply-tier2-llm-schedule.sh")

    assert "--from-file=tier2_full_scoring_runner.py=\"$runner\"" in script
    assert "--dry-run=client -o yaml | kubectl apply -f -" in script
    assert "kubectl -n \"$namespace\" apply -f \"$manifest\"" in script
