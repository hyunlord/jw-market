from __future__ import annotations

from pathlib import Path

from pipeline.scripts.ingest_hook import publish_runner
from pipeline.scripts.ingest_hook.job_launcher import publish_job_name


IDENTITY = ("2026-07", "ubist", "a" * 64)


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
