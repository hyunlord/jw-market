from __future__ import annotations

import pytest

from pipeline.scripts.ingest_hook.category_map import (
    ActivationKind,
    CSD_CHANNEL_E2E_STAGES,
    resolve_category,
)
from pipeline.scripts.ingest_hook import config, csd_channel_publish_runner, job_runner, publish_runner


def test_activation_kind_keeps_numeric_and_two_table_paths_separate() -> None:
    assert resolve_category("ubist").activation_kind is ActivationKind.UBIST_NUMERIC
    assert resolve_category("iqvia_csd_channel").activation_kind is ActivationKind.CSD_CHANNEL
    assert resolve_category("iqvia_nsa").activation_kind is ActivationKind.NONE
    assert resolve_category("iqvia_csd_keyword").activation_kind is ActivationKind.NONE


def test_csd_channel_progress_contract_has_seven_named_stages() -> None:
    assert CSD_CHANNEL_E2E_STAGES == (
        ("g3", "G3"),
        ("load", "적재"),
        ("load_verify", "적재 검증"),
        ("awaiting_approval", "승인 대기"),
        ("mart_publish", "CSD 원천·스테이지 게시"),
        ("context_bridge", "컨텍스트 브리지"),
        ("dashboard", "대시보드"),
    )


def test_csd_channel_remains_production_disabled() -> None:
    spec = resolve_category("iqvia_csd_channel")

    assert spec.production_load_supported is False
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
