from __future__ import annotations

from pathlib import Path

from pipeline.scripts.crawler.crawl_temporal_contract import ACTIVITY_POLICIES


def test_activity_policies_cover_every_stage_with_measured_timeouts() -> None:
    assert ACTIVITY_POLICIES["capture_exposure_baseline"].start_to_close_seconds == 900
    assert ACTIVITY_POLICIES["tier1_collect"].start_to_close_seconds == 10_800
    assert ACTIVITY_POLICIES["tier1_classify"].start_to_close_seconds == 1_800
    assert ACTIVITY_POLICIES["tier2_collect"].start_to_close_seconds == 28_800
    assert ACTIVITY_POLICIES["tier2_classify_and_refresh"].start_to_close_seconds == 7_200
    assert all(policy.heartbeat_seconds <= 300 for policy in ACTIVITY_POLICIES.values())
    assert all(policy.maximum_attempts == 2 for policy in ACTIVITY_POLICIES.values())


def test_temporal_runtime_is_sequential_and_validation_is_non_retryable() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "pipeline/scripts/crawler/temporal_crawl_daily.py").read_text(
        encoding="utf-8"
    )

    assert "for stage in ACTIVITY_STAGES" in source
    assert "await workflow.execute_activity" in source
    assert "non_retryable=True" in source
    assert "activity.heartbeat" in source
    assert "asyncio.create_subprocess_exec" in source
    assert "capture_exposure_baseline" in source
    assert "asyncio.to_thread" in source
    assert '"stage_timeout"' in source
    assert "resolve_execution_config(config, temporal_run_id=workflow.info().run_id)" in source


def test_shadow_manifest_cannot_create_a_schedule_or_cronjob() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "kind: Deployment" in manifest
    assert "kind: CronJob" not in manifest
    assert "kind: Schedule" not in manifest
    assert "-canonical" not in manifest
    assert "jw-market-crawl-temporal-shadow-v1" in manifest


def test_shadow_manifest_uses_an_isolated_state_claim() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "jw-crawl-temporal-shadow-state" in manifest
    assert "jw-crawl-chain-state" not in manifest


def test_shadow_manifest_disables_tier2_llm_calls() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: CRAWL_CHAIN_LLM_CALL_LIMIT" in manifest
    assert 'value: "0"' in manifest


def test_shadow_manifest_uses_the_live_tier1_classifier_endpoint() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/temporal-crawl-shadow-worker.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: WF196_DIRECT_RUN_URL" in manifest
    assert "http://workflow-196.llmops.svc.cluster.local:8080/run/v2" in manifest


def test_production_worker_is_separate_and_unbounded() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "deploy/k8s/crawler/temporal-crawl-worker.yaml"

    assert path.is_file()
    manifest = path.read_text(encoding="utf-8")
    assert "name: jw-market-crawl-temporal-worker" in manifest
    assert "name: jw-crawl-temporal-state" in manifest
    assert "value: jw-market-crawl-temporal-v1" in manifest
    assert "name: CRAWL_CHAIN_LLM_CALL_LIMIT" in manifest
    assert 'value: "100"' in manifest
    assert "name: CRAWL_CHAIN_LLM_MAX_COST_KRW" in manifest
    assert 'value: "339.00"' in manifest
    assert "shadow" not in manifest.lower()
    assert "CRAWL_CHAIN_TIER1_SITES" not in manifest
    assert "CRAWL_CHAIN_TIER1_MAX_ARTICLES" not in manifest
    assert "CRAWL_CHAIN_TIER2_SITES" not in manifest
    assert "CRAWL_CHAIN_TIER2_DAYS" not in manifest
    assert "CRAWL_CHAIN_TIER2_MAX_PAGES_PER_SITE" not in manifest
    assert "CRAWL_CHAIN_TIER2_MAX_LINKS_PER_PAGE" not in manifest
    assert "CRAWL_CHAIN_TIER2_MAX_ARTICLES" not in manifest
    assert "CRAWL_CHAIN_TIER2_LIMIT_BRANDS" not in manifest
