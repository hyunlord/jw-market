from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from pipeline.scripts.ingest_hook import (
    csd_channel_activation,
    csd_channel_publish_runner,
    csd_keyword_publish_runner,
)
from pipeline.scripts.ingest_hook.job_launcher import publish_job_name
from pipeline.scripts.ingest_hook.ledger import STATUS_PUBLISH_RUNNING


def _ledger_and_candidate(
    *, category: str, manifest_sha: str, build_run_id: str, publish_run_id: str,
    payload: dict[str, object],
) -> Mock:
    job_name = publish_job_name(category, manifest_sha, publish_run_id)
    ledger = Mock()
    ledger.status.return_value = SimpleNamespace(
        status=STATUS_PUBLISH_RUNNING,
        job_name=job_name,
    )
    ledger.prepared_candidate.return_value = SimpleNamespace(
        build_run_id=build_run_id,
        publish_job_name=job_name,
        payload=payload,
        prepared_at="2026-08-08 14:35:12",
    )
    return ledger


def test_channel_complete_signal_identifies_published_schema_and_time(monkeypatch) -> None:
    category = "iqvia_csd_channel"
    manifest_sha = "a" * 64
    build_run_id = "20260808143435479712"
    publish_run_id = "20260808154619170188"
    ledger = _ledger_and_candidate(
        category=category,
        manifest_sha=manifest_sha,
        build_run_id=build_run_id,
        publish_run_id=publish_run_id,
        payload={
            "csd_activation_plan": {},
            "csd_candidate_evidence": {},
            "mode": "production",
            "rows_loaded": 397146,
        },
    )
    plan = SimpleNamespace(
        raw=SimpleNamespace(live=SimpleNamespace(table="raw_csd_channel_dynamics")),
        stage=SimpleNamespace(live=SimpleNamespace(table="csd_channel_dynamics_stage")),
    )
    evidence = SimpleNamespace(
        raw=SimpleNamespace(row_count=397146),
        stage=SimpleNamespace(row_count=397146),
        periods=SimpleNamespace(complete_quarters=("2025-Q3",)),
    )
    published = False

    def stamp() -> str:
        return "2026-08-09 00:30:01" if published else "2026-08-09 00:30:00"

    def publish_candidate(*_args) -> csd_channel_activation.SwapVerdict:
        nonlocal published
        published = True
        return csd_channel_activation.SwapVerdict.APPLIED

    signal: dict[str, object] = {}
    monkeypatch.setattr(csd_channel_publish_runner, "_stamp", stamp)
    monkeypatch.setattr(
        csd_channel_publish_runner.config,
        "csd_channel_live_schemas",
        lambda *, mode: ("jw_brand_activity_raw_stage", "jw_brand_activity_stage"),
    )
    monkeypatch.setattr(csd_channel_publish_runner.config, "open_csd_channel_connection", Mock)
    monkeypatch.setattr(csd_channel_publish_runner, "acquire_writer_lock", Mock())
    monkeypatch.setattr(csd_channel_publish_runner, "_release_writer_lock_preserving_primary", Mock())
    monkeypatch.setattr(csd_channel_publish_runner.csd_channel_activation, "plan_from_payload", lambda _raw: plan)
    monkeypatch.setattr(csd_channel_publish_runner.csd_channel_activation, "evidence_from_payload", lambda _raw: evidence)
    monkeypatch.setattr(csd_channel_publish_runner.csd_channel_activation, "validate_plan_scope", Mock())
    monkeypatch.setattr(csd_channel_publish_runner.csd_channel_activation, "validate_candidate", lambda *_args: evidence)
    monkeypatch.setattr(csd_channel_publish_runner.csd_channel_activation, "publish_candidate", publish_candidate)
    monkeypatch.setattr(csd_channel_publish_runner, "_emit_completion_signal", lambda **kwargs: signal.update(kwargs))

    assert csd_channel_publish_runner.run(
        ledger=ledger,
        epoch="2025-10",
        category=category,
        manifest_sha=manifest_sha,
        build_run_id=build_run_id,
        publish_run_id=publish_run_id,
    ) == 0
    assert signal["target_schema"] == "jw_brand_activity_stage"
    assert signal["published_at"] == "2026-08-09 00:30:01"


def test_keyword_complete_signal_identifies_published_schema_and_time(monkeypatch) -> None:
    category = "iqvia_csd_keyword"
    manifest_sha = "b" * 64
    build_run_id = "20260808143437153992"
    publish_run_id = "20260808144302959389"
    ledger = _ledger_and_candidate(
        category=category,
        manifest_sha=manifest_sha,
        build_run_id=build_run_id,
        publish_run_id=publish_run_id,
        payload={
            "keyword_activation_plan": {},
            "keyword_candidate_evidence": {},
            "mode": "production",
            "rows_loaded": 9512,
        },
    )
    plan = SimpleNamespace(
        run_id=build_run_id,
        raw=SimpleNamespace(live=SimpleNamespace(table="raw_keyword_events")),
        stage=SimpleNamespace(live=SimpleNamespace(table="km_keyword_event_stage")),
    )
    evidence = SimpleNamespace(
        raw_rows=9512,
        stage_rows=9512,
        min_period="2025-07",
        max_period="2025-10",
    )
    published = False

    def stamp() -> str:
        return "2026-08-09 00:31:01" if published else "2026-08-09 00:31:00"

    def publish_candidate(*_args) -> None:
        nonlocal published
        published = True

    signal: dict[str, object] = {}
    monkeypatch.setattr(csd_keyword_publish_runner, "_stamp", stamp)
    monkeypatch.setattr(
        csd_keyword_publish_runner.config,
        "csd_keyword_live_schemas",
        lambda: ("jw_brand_activity_raw_stage", "jw_brand_activity_stage"),
    )
    monkeypatch.setattr(csd_keyword_publish_runner.config, "open_csd_channel_connection", Mock)
    monkeypatch.setattr(csd_keyword_publish_runner.config, "open_mart_connection", Mock)
    monkeypatch.setattr(csd_keyword_publish_runner, "acquire_writer_lock", Mock())
    monkeypatch.setattr(csd_keyword_publish_runner, "_release_writer_lock_preserving_primary", Mock())
    monkeypatch.setattr(
        csd_keyword_publish_runner.csd_keyword_activation,
        "plan_from_payload",
        lambda _raw, **_kwargs: plan,
    )
    monkeypatch.setattr(csd_keyword_publish_runner.csd_keyword_activation, "evidence_from_payload", lambda _raw: evidence)
    monkeypatch.setattr(csd_keyword_publish_runner.csd_keyword_activation, "validate_candidate", lambda *_args: evidence)
    monkeypatch.setattr(csd_keyword_publish_runner.csd_keyword_activation, "publish_candidate", publish_candidate)
    monkeypatch.setattr(csd_keyword_publish_runner, "_emit_completion_signal", lambda **kwargs: signal.update(kwargs))

    assert csd_keyword_publish_runner.run(
        ledger=ledger,
        epoch="2025-10",
        category=category,
        manifest_sha=manifest_sha,
        build_run_id=build_run_id,
        publish_run_id=publish_run_id,
    ) == 0
    assert signal["target_schema"] == "jw_brand_activity_stage"
    assert signal["published_at"] == "2026-08-09 00:31:01"
    recorded_stages = [call.kwargs["stage"] for call in ledger.record_stage.call_args_list]
    assert recorded_stages[-2:] == ["topic_extraction", "dashboard"]
