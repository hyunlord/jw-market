from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.scripts.ingest_hook import publish_runner
from pipeline.scripts.ingest_hook.job_launcher import publish_job_name
from pipeline.scripts.ingest_hook.ubist_mart_activation import (
    BuildTableFingerprint,
    CorpusCandidate,
    CorpusInventory,
    NUMERIC_TABLES,
)


IDENTITY = ("2026-07", "ubist", "a" * 64)


def _integrity_payload() -> dict:
    return {
        "candidate_integrity": {
            "file_count": 2,
            "total_bytes": 20,
            "manifest_sha": "c" * 64,
        },
        "build_table_integrity": [
            {"table": table, "row_count": 1, "crc_sum": 2, "crc_xor": 3}
            for table in NUMERIC_TABLES
        ],
    }


def test_publish_rejects_changed_candidate_before_promotion(monkeypatch, tmp_path) -> None:
    payload = _integrity_payload()
    corpus = CorpusCandidate(tmp_path / "live", tmp_path / "candidate", tmp_path / "backup")
    monkeypatch.setattr(
        publish_runner, "inventory_corpus",
        lambda _root: CorpusInventory(2, 21, "d" * 64),
    )
    monkeypatch.setattr(
        publish_runner, "fingerprint_build_tables",
        lambda _conn, _schema: tuple(
            BuildTableFingerprint(table, 1, 2, 3) for table in NUMERIC_TABLES
        ),
    )

    with pytest.raises(RuntimeError, match="candidate corpus integrity"):
        publish_runner._verify_publish_integrity(payload, corpus, object(), "build_db")


def test_publish_rejects_changed_build_tables_before_promotion(monkeypatch, tmp_path) -> None:
    payload = _integrity_payload()
    corpus = CorpusCandidate(tmp_path / "live", tmp_path / "candidate", tmp_path / "backup")
    monkeypatch.setattr(
        publish_runner, "inventory_corpus",
        lambda _root: CorpusInventory(2, 20, "c" * 64),
    )
    monkeypatch.setattr(
        publish_runner, "fingerprint_build_tables",
        lambda _conn, _schema: tuple(
            BuildTableFingerprint(table, 9 if index == 0 else 1, 2, 3)
            for index, table in enumerate(NUMERIC_TABLES)
        ),
    )

    with pytest.raises(RuntimeError, match="build-table integrity"):
        publish_runner._verify_publish_integrity(payload, corpus, object(), "build_db")


def test_publish_failure_recovers_candidate_with_build_run_identity(
    sqlite_ledger, monkeypatch, tmp_path
) -> None:
    live_root = tmp_path / "ubist"
    candidate_root = tmp_path / ".ubist_candidate_build-run"
    backup_root = tmp_path / ".ubist_backup_build-run"
    journal = tmp_path / ".ubist_activation_build-run.json"
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={
            "mode": "shadow",
            "source_db": "source_db",
            "target_db": "jw_mart_ingest_shadow_test",
            "build_db": "build_db",
            "live_root": str(live_root),
            "candidate_root": str(candidate_root),
            "backup_root": str(backup_root),
            "activation_journal": str(journal),
            "baseline_manifest_sha": "f" * 64,
            "baseline_live_snapshot": [],
            "row_counts": {},
            "periods": ["2026-07"],
            "automatic_publish": {
                "source_sets": {
                    "load": {
                        "sha256": "e" * 64,
                        "relative_paths": ["source.xlsx"],
                        "rows": 10,
                        "periods": ["2026-07"],
                    },
                    "publish": None,
                }
            },
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    expected_publish_job = publish_job_name("ubist", IDENTITY[2], "publish-run")
    assert sqlite_ledger.mark_publish_running(
        *IDENTITY,
        build_run_id="build-run",
        publish_job_name=expected_publish_job,
        approved_by="pl@example.com",
        approved_at="2026-08-04T01:00:00+00:00",
    )

    calls: list[object] = []

    class Connection:
        def close(self):
            calls.append("close")

    monkeypatch.setattr(publish_runner.config, "open_mart_connection", lambda *_args: Connection())
    monkeypatch.setattr(publish_runner, "acquire_writer_lock", lambda *_args, **_kwargs: calls.append("lock"))
    monkeypatch.setattr(publish_runner, "_verify_publish_integrity", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "_release_writer_lock_preserving_primary",
        lambda *_args, **_kwargs: calls.append("unlock"),
    )
    monkeypatch.setattr(publish_runner, "require_corpus_manifest", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "fingerprint_untouched_sources",
        lambda *_args, **_kwargs: publish_runner._snapshot([]),
    )
    monkeypatch.setattr(publish_runner, "promote_candidate_corpus", lambda *_args: calls.append("corpus"))
    monkeypatch.setattr(publish_runner, "update_activation_journal", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "_measure_publish_source_set",
        lambda _category, evidence: evidence,
    )

    def fail_publish(*_args, **kwargs):
        calls.append(("publish_run_id", kwargs["run_id"]))
        raise ValueError("injected post-corpus publish failure")

    monkeypatch.setattr(publish_runner, "publish_shadow", fail_publish)
    monkeypatch.setattr(
        publish_runner,
        "recover_incomplete_activations",
        lambda *_args, **_kwargs: calls.append("recover") or (Path(journal),),
        raising=False,
    )
    monkeypatch.setattr(
        publish_runner,
        "complete_recovery",
        lambda _paths: calls.append("recovery-complete"),
        raising=False,
    )
    monkeypatch.setattr(publish_runner, "validate_shadow_publish", lambda *_args: calls.append("validate"))

    result = publish_runner.run(
        ledger=sqlite_ledger,
        epoch=IDENTITY[0],
        category=IDENTITY[1],
        manifest_sha=IDENTITY[2],
        build_run_id="build-run",
        publish_run_id="publish-run",
    )

    assert result == 1
    assert ("publish_run_id", "build-run") in calls
    assert calls.index("recover") < calls.index("recovery-complete")
    assert sqlite_ledger.status(*IDENTITY).status == "failed"
    assert not any(
        event.stage == "dashboard" and event.status == "complete"
        for event in sqlite_ledger.stage_events(*IDENTITY)
    )


def test_publish_marks_corpus_promotion_started_before_rename(
    sqlite_ledger, monkeypatch, tmp_path
) -> None:
    live_root = tmp_path / "ubist"
    candidate_root = tmp_path / ".ubist_candidate_build-run"
    backup_root = tmp_path / ".ubist_backup_build-run"
    journal = tmp_path / ".ubist_activation_build-run.json"
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={
            "mode": "shadow",
            "source_db": "source_db",
            "target_db": "jw_mart_ingest_shadow_test",
            "build_db": "build_db",
            "live_root": str(live_root),
            "candidate_root": str(candidate_root),
            "backup_root": str(backup_root),
            "activation_journal": str(journal),
            "baseline_manifest_sha": "f" * 64,
            "baseline_live_snapshot": [],
            "row_counts": {},
            "periods": ["2026-07"],
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    expected_publish_job = publish_job_name("ubist", IDENTITY[2], "publish-run")
    assert sqlite_ledger.mark_publish_running(
        *IDENTITY,
        build_run_id="build-run",
        publish_job_name=expected_publish_job,
        approved_by="pl@example.com",
        approved_at="2026-08-04T01:00:00+00:00",
    )

    class Connection:
        def close(self):
            return None

    phases: list[str] = []
    recovered: list[str] = []
    monkeypatch.setattr(publish_runner.config, "open_mart_connection", lambda *_args: Connection())
    monkeypatch.setattr(publish_runner, "acquire_writer_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_runner, "_verify_publish_integrity", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "_release_writer_lock_preserving_primary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(publish_runner, "require_corpus_manifest", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "fingerprint_untouched_sources",
        lambda *_args, **_kwargs: publish_runner._snapshot([]),
    )
    monkeypatch.setattr(
        publish_runner,
        "update_activation_journal",
        lambda _path, phase: phases.append(phase),
    )

    def crash_before_rename(_corpus):
        assert phases[-1] == "corpus_promotion_started"
        raise RuntimeError("injected crash before corpus rename")

    monkeypatch.setattr(publish_runner, "promote_candidate_corpus", crash_before_rename)
    monkeypatch.setattr(
        publish_runner,
        "recover_incomplete_activations",
        lambda *_args, **_kwargs: recovered.append("recover") or (journal,),
    )
    monkeypatch.setattr(publish_runner, "complete_recovery", lambda _paths: None)
    monkeypatch.setattr(publish_runner, "validate_shadow_publish", lambda *_args: None)

    result = publish_runner.run(
        ledger=sqlite_ledger,
        epoch=IDENTITY[0],
        category=IDENTITY[1],
        manifest_sha=IDENTITY[2],
        build_run_id="build-run",
        publish_run_id="publish-run",
    )

    assert result == 1
    assert phases[0] == "corpus_promotion_started"
    assert recovered == ["recover"]


def test_production_publish_binds_dashboard_to_real_refresh(
    sqlite_ledger, monkeypatch, tmp_path
) -> None:
    live_root = tmp_path / "ubist"
    candidate_root = tmp_path / ".ubist_candidate_build-run"
    backup_root = tmp_path / ".ubist_backup_build-run"
    journal = tmp_path / ".ubist_activation_build-run.json"
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={
            "mode": "production",
            "source_db": "source_db",
            "target_db": "target_db",
            "build_db": "build_db",
            "live_root": str(live_root),
            "candidate_root": str(candidate_root),
            "backup_root": str(backup_root),
            "activation_journal": str(journal),
            "baseline_manifest_sha": "f" * 64,
            "baseline_live_snapshot": [],
            "row_counts": {"mart_general_brand_metric": 10},
            "periods": ["2026-07"],
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    publish_job = publish_job_name("ubist", IDENTITY[2], "publish-run")
    assert sqlite_ledger.mark_publish_running(
        *IDENTITY,
        build_run_id="build-run",
        publish_job_name=publish_job,
        approved_by="pl@example.com",
        approved_at="2026-08-04T01:00:00+00:00",
    )

    connection = SimpleNamespace(close=lambda: None)
    refresh_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(publish_runner.config, "open_mart_connection", lambda *_args: connection)
    monkeypatch.setattr(publish_runner, "acquire_writer_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_runner, "_verify_publish_integrity", lambda *_args: None)
    monkeypatch.setattr(publish_runner, "_release_writer_lock_preserving_primary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_runner, "require_corpus_manifest", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "fingerprint_untouched_sources",
        lambda *_args, **_kwargs: publish_runner._snapshot([]),
    )
    monkeypatch.setattr(publish_runner, "promote_candidate_corpus", lambda *_args: None)
    monkeypatch.setattr(publish_runner, "publish_shadow", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_runner, "update_activation_journal", lambda *_args: None)
    monkeypatch.setattr(
        publish_runner,
        "_run_commands_with_writer_lock",
        lambda _label, argv, **_kwargs: refresh_calls.append(argv),
    )
    monkeypatch.setattr(publish_runner, "_emit_completion_signal", lambda **_kwargs: None)
    monkeypatch.setattr(
        publish_runner,
        "_measure_publish_source_set",
        lambda _category, evidence: evidence,
    )
    monkeypatch.setattr(
        publish_runner,
        "_source_set_from_contract",
        lambda _payload: SimpleNamespace(
            sha256="e" * 64,
            relative_paths=("source.xlsx",),
        ),
    )
    monkeypatch.setattr(
        publish_runner,
        "_mark_complete_after_required_stages",
        lambda **kwargs: kwargs["ledger"].mark_complete(
            *kwargs["identity"], row_counts=kwargs["row_counts"]
        ),
    )

    assert publish_runner.run(
        ledger=sqlite_ledger,
        epoch=IDENTITY[0],
        category=IDENTITY[1],
        manifest_sha=IDENTITY[2],
        build_run_id="build-run",
        publish_run_id="publish-run",
    ) == 0

    events = sqlite_ledger.stage_events(*IDENTITY)
    refresh = next(event for event in events if event.stage == "refresh")
    dashboard = next(event for event in events if event.stage == "dashboard")
    assert refresh_calls
    assert dashboard.status == "complete"
    assert dashboard.started_at == refresh.started_at
    assert dashboard.finished_at == refresh.finished_at
    assert dashboard.duration_ms == refresh.duration_ms
    assert "target_schema=target_db" in str(dashboard.reason)
