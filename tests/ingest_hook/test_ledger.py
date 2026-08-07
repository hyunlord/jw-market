"""ingest_ledger semantics: idempotency (G-3 unit), serialisation, baselines."""
from __future__ import annotations

import sqlite3

import pytest

from pipeline.scripts.ingest_hook import ledger as ledger_module

IDENTITY = ("2026-07", "ubist", "a" * 64)


def test_mysql_ledger_schema_indexes_post_gate_run_lookup() -> None:
    assert "KEY idx_ledger_run_id_id (run_id, id)" in ledger_module._DDL_MYSQL


def test_same_webhook_three_times_runs_once(sqlite_ledger):
    first = sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    second = sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    third = sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    assert first.action == "queued"
    assert (second.action, third.action) == ("noop", "noop")


def test_noop_persists_through_running_and_complete(sqlite_ledger):
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="job-1", run_id="r1")
    assert sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json").action == "noop"
    sqlite_ledger.mark_complete(*IDENTITY, row_counts={"data.csv": 6})
    assert sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json").action == "noop"


def test_failed_submission_can_requeue(sqlite_ledger):
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="job-1", run_id="r1")
    sqlite_ledger.mark_failed(*IDENTITY, reason="G3Error: sha mismatch")
    decision = sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    assert decision.action == "queued"
    assert sqlite_ledger.status(*IDENTITY).status == "queued"


def test_category_serialisation_counts_only_running(sqlite_ledger):
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/a.json")
    assert sqlite_ledger.running_in_category("ubist") == 0
    sqlite_ledger.mark_running(*IDENTITY, job_name="job-1", run_id="r1")
    assert sqlite_ledger.running_in_category("ubist") == 1
    assert sqlite_ledger.running_in_category("iqvia") == 0  # other categories parallel


def test_mark_running_is_a_queued_to_running_compare_and_set(sqlite_ledger):
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/a.json")

    assert sqlite_ledger.mark_running(
        *IDENTITY, job_name="job-1", run_id="run-1"
    ) is True
    assert sqlite_ledger.mark_running(
        *IDENTITY, job_name="job-2", run_id="run-2"
    ) is False

    entry = sqlite_ledger.status(*IDENTITY)
    assert entry.status == "running"
    assert entry.job_name == "job-1"
    assert entry.run_id == "run-1"


def test_next_queued_is_fifo(sqlite_ledger):
    sqlite_ledger.receive("2026-06", "ubist", "b" * 64, manifest_path="/x/b.json")
    sqlite_ledger.receive("2026-07", "ubist", "c" * 64, manifest_path="/x/c.json")
    entry = sqlite_ledger.next_queued("ubist")
    assert entry.manifest_sha == "b" * 64


def test_previous_complete_total_baseline(sqlite_ledger):
    sqlite_ledger.receive("2026-06", "ubist", "d" * 64, manifest_path="/x/d.json")
    sqlite_ledger.mark_complete("2026-06", "ubist", "d" * 64, row_counts={"a.csv": 4, "b.csv": 2})
    assert sqlite_ledger.previous_complete_total("ubist", before_epoch="2026-07") == 6
    assert sqlite_ledger.previous_complete_total("ubist", before_epoch="2026-06") is None
    assert sqlite_ledger.previous_complete_total("iqvia", before_epoch="2026-07") is None


def test_uploaded_by_recorded_and_refreshed_on_requeue(sqlite_ledger):
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json", uploaded_by="a@jw.example")
    assert sqlite_ledger.status(*IDENTITY).uploaded_by == "a@jw.example"
    sqlite_ledger.mark_failed(*IDENTITY, reason="boom")
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json", uploaded_by="b@jw.example")
    assert sqlite_ledger.status(*IDENTITY).uploaded_by == "b@jw.example"


def test_awaiting_approval_holds_identity_and_same_category_slot(sqlite_ledger) -> None:
    # Given: a completed build has prepared an exact publish candidate.
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")

    # When: the runner marks the candidate as awaiting explicit publish approval.
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={
            "epoch": IDENTITY[0],
            "category": IDENTITY[1],
            "manifest_sha": IDENTITY[2],
            "run_id": "build-run",
            "activation_journal": "/market-output/.ubist_activation_build_run.json",
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )

    # Then: duplicate webhooks are no-ops and the next same-category row is blocked.
    assert sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json").action == "noop"
    sqlite_ledger.receive("2026-08", "ubist", "b" * 64, manifest_path="/x/next.json")
    assert sqlite_ledger.next_queued("ubist").manifest_sha == "b" * 64
    assert sqlite_ledger.claim_queued(
        "2026-08",
        "ubist",
        "b" * 64,
        job_name="next-job",
        run_id="next-run",
    ) is False
    assert sqlite_ledger.prepared_candidate(*IDENTITY).payload["run_id"] == "build-run"


def test_publish_reservation_rolls_back_ledger_when_candidate_update_fails(
    sqlite_ledger,
) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    sqlite_ledger._conn.execute(
        "CREATE TRIGGER reject_publish_candidate_update "
        "BEFORE UPDATE OF publish_job_name ON ingest_publish_candidate "
        "BEGIN SELECT RAISE(ABORT, 'injected candidate update failure'); END"
    )
    sqlite_ledger._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected candidate update failure"):
        sqlite_ledger.mark_publish_running(
            *IDENTITY,
            build_run_id="build-run",
            publish_job_name="publish-job",
            approved_by="pl@example.com",
            approved_at="2026-08-04T01:00:00+00:00",
        )

    assert sqlite_ledger.status(*IDENTITY).status == "awaiting_approval"
    assert sqlite_ledger.prepared_candidate(*IDENTITY).publish_job_name is None


def test_publish_reservation_rechecks_expiry_inside_locked_candidate_read(
    sqlite_ledger,
) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-04T01:00:00+00:00",
    )

    changed = sqlite_ledger.mark_publish_running(
        *IDENTITY,
        build_run_id="build-run",
        publish_job_name="publish-job",
        approved_by="pl@example.com",
        approved_at="2026-08-04T01:00:00.000001+00:00",
    )

    assert changed is False
    assert sqlite_ledger.status(*IDENTITY).status == "awaiting_approval"
    assert sqlite_ledger.prepared_candidate(*IDENTITY).publish_job_name is None


def test_prepare_candidate_rolls_back_when_candidate_insert_fails(sqlite_ledger) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build-job", run_id="build-run")
    sqlite_ledger._conn.execute(
        "CREATE TRIGGER reject_publish_candidate_insert "
        "BEFORE INSERT ON ingest_publish_candidate "
        "BEGIN SELECT RAISE(ABORT, 'injected candidate insert failure'); END"
    )
    sqlite_ledger._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected candidate insert failure"):
        sqlite_ledger.mark_awaiting_approval(
            *IDENTITY,
            run_id="build-run",
            candidate={"run_id": "build-run"},
            prepared_at="2026-08-04T00:00:00+00:00",
            expires_at="2026-08-05T00:00:00+00:00",
        )

    assert sqlite_ledger.status(*IDENTITY).status == "running"
    assert sqlite_ledger.prepared_candidate(*IDENTITY) is None


def test_rearm_candidate_reset_and_audit_transition_are_atomic(sqlite_ledger) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY, run_id="build-run", candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
    )
    sqlite_ledger.mark_publish_running(
        *IDENTITY, build_run_id="build-run", publish_job_name="publish",
        approved_by="pl", approved_at="2026-08-04T00:01:00Z",
    )
    sqlite_ledger.mark_failed(*IDENTITY, reason="1105")
    sqlite_ledger._conn.execute(
        "CREATE TRIGGER reject_rearm_audit BEFORE INSERT ON ingest_status_transition "
        "WHEN NEW.source='audited_publish_rearm' "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    sqlite_ledger._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        sqlite_ledger.rearm_failed_candidate(
            *IDENTITY, build_run_id="build-run", actor="operator",
            evidence={"file_count": 67},
        )

    assert sqlite_ledger.status(*IDENTITY).status == "failed"
    assert sqlite_ledger.prepared_candidate(*IDENTITY).publish_job_name == "publish"


def test_rearm_candidate_rechecks_expiry_inside_transaction(sqlite_ledger) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/x/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="build", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *IDENTITY, run_id="build-run", candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00Z", expires_at="2000-01-01T00:00:00Z",
    )
    sqlite_ledger.mark_publish_running(
        *IDENTITY, build_run_id="build-run", publish_job_name="publish",
        approved_by="pl", approved_at="1999-01-01T00:01:00Z",
    )
    sqlite_ledger.mark_failed(*IDENTITY, reason="1105")

    changed = sqlite_ledger.rearm_failed_candidate(
        *IDENTITY, build_run_id="build-run", actor="operator",
        evidence={"file_count": 67},
    )

    assert changed is False
    assert sqlite_ledger.status(*IDENTITY).status == "failed"
    assert sqlite_ledger.prepared_candidate(*IDENTITY).publish_job_name == "publish"
