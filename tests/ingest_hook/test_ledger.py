"""ingest_ledger semantics: idempotency (G-3 unit), serialisation, baselines."""
from __future__ import annotations

IDENTITY = ("2026-07", "ubist", "a" * 64)


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
