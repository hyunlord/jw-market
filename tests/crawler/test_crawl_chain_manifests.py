from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CRAWLER_DIR = REPO_ROOT / "deploy" / "k8s" / "crawler"


def _yaml_documents(name: str) -> list[dict[str, object]]:
    text = (CRAWLER_DIR / name).read_text(encoding="utf-8")
    return [document for document in yaml.safe_load_all(text) if document]


def test_chain_manifest_serializes_four_stages_and_persists_receipts() -> None:
    # Given: the proposed crawl-chain manifest.
    documents = _yaml_documents("crawl-chain-cronjob.yaml")
    cronjob = next(document for document in documents if document["kind"] == "CronJob")
    spec = cronjob["spec"]
    pod_spec = spec["jobTemplate"]["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    args = container["args"][0]

    # When/Then: one active schedule owns ordering and durable state.
    assert cronjob["metadata"]["name"] == "jw-crawl-chain-daily"
    assert spec["schedule"] == "10 18 * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["startingDeadlineSeconds"] == 900
    assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 43200
    assert spec["suspend"] is True
    assert "pipeline/scripts/crawler/crawl_chain.py" in args
    assert pod_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "jw-crawl-chain-state"
    assert container["volumeMounts"][0]["mountPath"] == "/var/lib/jw-crawl-chain"

    helper = (CRAWLER_DIR / "apply-crawl-chain.sh").read_text(encoding="utf-8")
    assert 'patch cronjob jw-crawl-chain-daily -p \'{"spec":{"suspend":false}}\'' in helper
    assert helper.index("crawl-tier1-cronjob.yaml") < helper.index(
        'patch cronjob jw-crawl-chain-daily'
    )
    assert helper.index("crawl-tier2-cronjob.yaml") < helper.index(
        'patch cronjob jw-crawl-chain-daily'
    )


def test_legacy_cronjobs_are_suspended_without_schedule_deletion() -> None:
    # Given: both legacy tracked manifests.
    tier1 = _yaml_documents("crawl-tier1-cronjob.yaml")[0]
    tier2 = _yaml_documents("crawl-tier2-cronjob.yaml")[0]

    # When/Then: rollback objects remain but cannot double-run after cutover.
    assert tier1["spec"]["suspend"] is True
    assert tier2["spec"]["suspend"] is True
    assert tier1["spec"]["schedule"] == "10 18 * * *"
    assert tier2["spec"]["schedule"] == "40 18 * * *"


def test_cache_refresh_remains_independent_at_0500_kst() -> None:
    # Given: the cache refresh manifest.
    path = REPO_ROOT / "deploy" / "k8s" / "cache-refresh" / "jw-cache-refresh-daily-cronjob.yaml"
    cache = yaml.safe_load(path.read_text(encoding="utf-8"))
    chain_text = (CRAWLER_DIR / "crawl-chain-cronjob.yaml").read_text(encoding="utf-8")

    # When/Then: cache is not a fifth chain stage and retains its schedule.
    assert cache["spec"]["schedule"] == "0 20 * * *"
    assert cache["spec"]["suspend"] is False
    assert "cache-refresh" not in chain_text


def test_stage_script_preserves_incremental_loader_and_category_refresh_order() -> None:
    # Given: the tracked implementation behind the generated runner ConfigMap.
    script = (REPO_ROOT / "pipeline" / "scripts" / "crawler" / "crawl_chain_steps.sh").read_text(
        encoding="utf-8"
    )

    # When/Then: existing incremental identities and the bounded LLM budget are
    # preserved, and the historical category repair remains the final action.
    assert "--processed-by workflow_196_rev5674" in script
    assert "--processed-by tier2_exact_rule_v1" in script
    sync_at = script.index("sync-events-raw")
    append_at = script.index("append-live")
    refresh_at = script.index("refresh-live-categories")
    assert sync_at < append_at < refresh_at
    assert 'CRAWL_CHAIN_LLM_CALL_LIMIT:-60' in script
    assert '--daily-call-limit "${llm_call_limit}" --max-cost-krw 203.40' in script
