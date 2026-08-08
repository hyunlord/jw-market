from __future__ import annotations

import pytest

from pipeline.scripts.ingest_hook.category_map import (
    ActivationKind,
    CSD_CHANNEL_E2E_STAGES,
    resolve_category,
)
from pipeline.scripts.ingest_hook import (
    config,
    csd_channel_publish_runner,
    csd_keyword_publish_runner,
    job_runner,
    publish_runner,
)


def test_activation_kind_keeps_numeric_and_two_table_paths_separate() -> None:
    assert resolve_category("ubist").activation_kind is ActivationKind.UBIST_NUMERIC
    assert resolve_category("iqvia_csd_channel").activation_kind is ActivationKind.CSD_CHANNEL
    assert resolve_category("iqvia_nsa").activation_kind is ActivationKind.IQVIA_NSA
    assert resolve_category("iqvia_csd_keyword").activation_kind is ActivationKind.CSD_KEYWORD


def test_csd_progress_contract_has_five_named_stages() -> None:
    assert CSD_CHANNEL_E2E_STAGES == (
        ("g3", "G3"),
        ("load", "적재"),
        ("mart_publish", "CSD 원천·스테이지 게시"),
        ("context_bridge", "컨텍스트 브리지"),
        ("dashboard", "대시보드"),
    )


@pytest.mark.parametrize("category", ["iqvia_csd_channel", "iqvia_csd_keyword"])
def test_csd_sources_are_production_enabled(category: str) -> None:
    spec = resolve_category(category)

    assert spec.production_load_supported is True
    assert spec.sigma_source is None
    assert spec.refresh_argv == ()


def test_publish_runner_dispatches_csd_without_entering_ubist_path(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return 23

    monkeypatch.setattr(csd_channel_publish_runner, "run", fake_run)

    result = publish_runner.run(
        ledger=object(),
        epoch="2026-05",
        category="iqvia_csd_channel",
        manifest_sha="a" * 64,
        build_run_id="build-run",
        publish_run_id="publish-run",
    )

    assert result == 23
    assert calls == [
        {
            "ledger": calls[0]["ledger"],
            "epoch": "2026-05",
            "category": "iqvia_csd_channel",
            "manifest_sha": "a" * 64,
            "build_run_id": "build-run",
            "publish_run_id": "publish-run",
        }
    ]


def test_publish_runner_dispatches_keyword_without_entering_ubist_path(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return 29

    monkeypatch.setattr(csd_keyword_publish_runner, "run", fake_run)

    result = publish_runner.run(
        ledger=object(),
        epoch="2026-05",
        category="iqvia_csd_keyword",
        manifest_sha="b" * 64,
        build_run_id="build-run",
        publish_run_id="publish-run",
    )

    assert result == 29
    assert calls[0]["category"] == "iqvia_csd_keyword"


def test_shadow_capability_is_separate_from_production_allowlist(monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CSD_CHANNEL_SHADOW_ACTIVATION, "1")
    monkeypatch.delenv(config.ENV_PRODUCTION_LOAD_CATEGORIES, raising=False)

    assert config.source_activation_enabled("iqvia_csd_channel", mode="shadow") is True
    assert config.source_activation_enabled("iqvia_csd_channel", mode="production") is False
    assert config.source_activation_enabled("ubist", mode="shadow") is False


def test_invalid_activation_allowlist_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_PRODUCTION_LOAD_CATEGORIES, "iqvia_csd_channel,iqvia_nsa")

    with pytest.raises(RuntimeError, match="unsupported production activation"):
        config.source_activation_enabled("iqvia_csd_channel", mode="production")


def test_production_activation_allowlist_supports_both_csd_sources(monkeypatch) -> None:
    monkeypatch.setenv(
        config.ENV_PRODUCTION_LOAD_CATEGORIES,
        "iqvia_csd_channel,iqvia_csd_keyword",
    )

    assert config.source_activation_enabled("iqvia_csd_channel", mode="production")
    assert config.source_activation_enabled("iqvia_csd_keyword", mode="production")


def test_expected_stages_keep_csd_out_of_numeric_mart() -> None:
    stages = {
        row["stage"]: row["applicable"]
        for row in job_runner.expected_stages(resolve_category("iqvia_csd_channel"))
    }

    assert stages["mart_build"] is False
    assert stages["sigma"] is False
    assert stages["post_gate"] is True
    assert stages["mart_publish"] is True
    assert stages["refresh"] is False


def test_expected_stages_include_nsa_mart_and_keyword_publish() -> None:
    nsa = {
        row["stage"]: row["applicable"]
        for row in job_runner.expected_stages(resolve_category("iqvia_nsa"))
    }
    keyword = {
        row["stage"]: row["applicable"]
        for row in job_runner.expected_stages(resolve_category("iqvia_csd_keyword"))
    }

    assert nsa["mart_build"] is True
    assert nsa["sigma"] is True
    assert nsa["mart_publish"] is True
    assert keyword["mart_build"] is False
    assert keyword["mart_publish"] is True


def test_e2e_commissioning_is_explicit_and_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("INGEST_E2E_COMMISSIONING", raising=False)
    assert config.e2e_commissioning() is False

    monkeypatch.setenv("INGEST_E2E_COMMISSIONING", "1")
    assert config.e2e_commissioning() is True

    monkeypatch.setenv("INGEST_E2E_COMMISSIONING", "yes")
    with pytest.raises(RuntimeError, match="INGEST_E2E_COMMISSIONING"):
        config.e2e_commissioning()


def test_keyword_live_schemas_are_environment_configurable(monkeypatch) -> None:
    monkeypatch.setenv(config.ENV_CSD_KEYWORD_RAW_SCHEMA, "jw_brand_activity_raw_stage")
    monkeypatch.setenv(config.ENV_CSD_KEYWORD_STAGE_SCHEMA, "jw_brand_activity_stage")

    assert config.csd_keyword_live_schemas() == (
        "jw_brand_activity_raw_stage",
        "jw_brand_activity_stage",
    )
