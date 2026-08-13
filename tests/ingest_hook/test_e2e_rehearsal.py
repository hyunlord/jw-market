"""Isolation rehearsals for the commissioning gates (zero production contact).

G-1  webhook -> ledger -> Job -> G3 pass -> staging load -> Σ gate -> complete
G-2  broken submissions -> zero rows loaded + failed status recorded
G-3  same webhook three times -> exactly one Job
G-4  webhook never fired -> daily sweep picks the manifest up and completes it
Everything runs on a tmp bucket + sqlite ledger + sqlite staging + fake k8s
transport; the mart DB, the cluster, and the serving backend are untouched.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.sweep import sweep
from ingest_fixtures import GOOD_ROWS, write_submission

IDENTITY_COLUMNS = ("epoch", "category", "manifest_sha")


@pytest.fixture
def service(sqlite_ledger, bucket, fake_transport) -> IngestService:
    return IngestService(
        sqlite_ledger,
        bucket,
        transport=fake_transport,
        inspect_transport=lambda _namespace, name: {
            "metadata": {"name": name},
            "status": {"active": 1},
        },
    )


@pytest.fixture
def client(service) -> TestClient:
    return TestClient(create_app(service))


def _webhook(client, manifest_path, bucket):
    return client.post(
        "/ingest/webhook", json={"manifest_path": str(manifest_path.relative_to(bucket))}
    )


# --------------------------------------------------------------------- G-1
def test_g1_end_to_end_rehearsal(client, service, bucket, tmp_path, fake_transport):
    manifest_path = write_submission(bucket)

    # webhook -> queued -> Job submitted (fake transport) -> running
    response = _webhook(client, manifest_path, bucket)
    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"] == "queued"
    assert payload["job_name"] == fake_transport.submitted[0][1]["metadata"]["name"]

    # the Job body carries the runner + manifest path (G3 is its first step)
    job_body = fake_transport.submitted[0][1]
    command = job_body["spec"]["template"]["spec"]["containers"][0]["command"]
    assert "pipeline.scripts.ingest_hook.stage_log_runner" in command

    # execute the Job's work inline in rehearsal mode
    staging_root = tmp_path / "staging"
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=service.ledger, rehearsal_root=staging_root
    )
    assert rc == 0

    # staging schema got the rows and the Σ gate reconciled
    conn = sqlite3.connect(str(staging_root / "staging.db"))
    count = conn.execute("SELECT COUNT(*) FROM ingest_staging_ubist").fetchone()[0]
    conn.close()
    assert count == len(GOOD_ROWS)

    # ledger reached complete with per-file row counts
    status = client.get(
        "/ingest/status",
        params={"epoch": payload["epoch"], "category": "ubist", "manifest_sha": payload["manifest_sha"]},
    ).json()
    assert status["status"] == "complete"
    entry = service.ledger.status(payload["epoch"], "ubist", payload["manifest_sha"])
    assert sum(entry.row_counts.values()) == len(GOOD_ROWS)


# --------------------------------------------------------------------- G-2
@pytest.mark.parametrize(
    "corruption",
    [
        {"sha_override": "0" * 64},                                    # sha mismatch
        {"rows": [], "declared_rows": 0},                              # zero rows
        {"header": ("period", "level", "name", "amount")},             # broken schema
        {"rows": [row for row in GOOD_ROWS if row[0] != "2026-07"]},     # requested epoch absent
    ],
)
def test_g2_rejected_submission_loads_nothing(sqlite_ledger, bucket, tmp_path, corruption):
    manifest_path = write_submission(bucket, **corruption)
    staging_root = tmp_path / "staging"

    rc = job_runner.run(manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=staging_root)

    assert rc == 1
    assert not (staging_root / "staging.db").exists(), "G3 must fail before any staging write"
    entries = [sqlite_ledger.status("2026-07", "ubist", sha) for sha in _all_shas(sqlite_ledger)]
    failed = [entry for entry in entries if entry and entry.status == "gate_failed"]
    assert failed and "G3Error" in failed[0].reason


def _all_shas(ledger) -> list[str]:
    cursor = ledger._execute("SELECT manifest_sha FROM ingest_ledger")  # test-only peek
    return [row[0] for row in cursor.fetchall()]


def test_g2_sigma_gate_rejects_broken_totals(sqlite_ledger, bucket, tmp_path):
    rows = [
        ("2026-07", "Class", "리바로", 10.0),
        ("2026-07", "Class", "리바로젯", 20.0),
        ("2026-07", "전체", "-", 99.0),  # whole != Σparts
    ]
    manifest_path = write_submission(bucket, rows=rows)
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "staging"
    )
    assert rc == 1
    entry = next(
        entry for entry in (sqlite_ledger.status("2026-07", "ubist", sha) for sha in _all_shas(sqlite_ledger))
        if entry is not None
    )
    assert entry.status == "gate_failed"
    assert "PG-1" in entry.reason


def test_g2_unknown_category_fails_closed(sqlite_ledger, bucket, tmp_path):
    manifest_path = write_submission(bucket, category="mystery")
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger, rehearsal_root=tmp_path / "staging"
    )
    assert rc == 1
    entry = next(
        entry for entry in (sqlite_ledger.status("2026-07", "mystery", sha) for sha in _all_shas(sqlite_ledger))
        if entry is not None
    )
    assert "UnknownCategoryError" in entry.reason


# --------------------------------------------------------------------- G-3
def test_g3_gate_same_webhook_three_times_one_job(client, bucket, fake_transport):
    manifest_path = write_submission(bucket)
    decisions = [_webhook(client, manifest_path, bucket).json()["decision"] for _ in range(3)]
    assert decisions == ["queued", "noop", "noop"]
    assert len(fake_transport.submitted) == 1, "exactly one Job for three identical webhooks"


def test_failed_submission_retry_uses_a_new_job_name(client, service, bucket, fake_transport):
    manifest_path = write_submission(bucket)
    first = _webhook(client, manifest_path, bucket).json()
    service.ledger.mark_failed(
        first["epoch"], first["category"], first["manifest_sha"], reason="injected failure"
    )

    second = _webhook(client, manifest_path, bucket).json()

    assert second["decision"] == "queued"
    assert second["job_name"] != first["job_name"]
    assert len(fake_transport.submitted) == 2


def test_job_submission_failure_does_not_leave_an_orphaned_queue(service, bucket):
    from pipeline.scripts.ingest_hook.contract import load_manifest

    manifest_path = write_submission(bucket)
    manifest = load_manifest(manifest_path)

    def reject(_path, _job):
        raise RuntimeError("injected Kubernetes 409")

    service.transport = reject

    with pytest.raises(RuntimeError, match="409"):
        service.receive_webhook(str(manifest_path.relative_to(bucket)))

    entry = service.ledger.status(
        manifest.epoch, manifest.category, manifest.manifest_sha
    )
    assert entry is not None
    assert entry.status == "failed"
    assert "job submission failed" in (entry.reason or "")


def test_same_category_serialises_distinct_submissions(client, bucket, fake_transport):
    first = write_submission(bucket, epoch="2026-06", rows=GOOD_ROWS[:3])
    second = write_submission(bucket, epoch="2026-07")
    assert _webhook(client, first, bucket).json()["job_name"] is not None
    # second submission queues but must NOT launch while the first is running
    assert _webhook(client, second, bucket).json()["job_name"] is None
    assert len(fake_transport.submitted) == 1


def test_webhook_exact_promotion_cannot_bypass_global_fifo(
    monkeypatch, service, bucket, fake_transport
):
    from pipeline.scripts.ingest_hook.contract import load_manifest

    older_path = write_submission(bucket, epoch="2026-05", rows=GOOD_ROWS[:3])
    newer_path = write_submission(bucket, epoch="2026-06")
    older = load_manifest(older_path)
    newer = load_manifest(newer_path)
    service.ledger.receive(
        older.epoch,
        older.category,
        older.manifest_sha,
        manifest_path=str(older_path.relative_to(bucket)),
        uploaded_by=older.uploaded_by,
    )

    monkeypatch.setenv("INGEST_WEBHOOK_PROMOTE_EXACT", "1")
    result = service.receive_webhook(str(newer_path.relative_to(bucket)))

    assert result["job_name"] is not None
    assert older.manifest_sha[:8] in result["job_name"]
    assert service.ledger.status(
        older.epoch, older.category, older.manifest_sha
    ).status == "running"
    assert service.ledger.status(
        newer.epoch, newer.category, newer.manifest_sha
    ).status == "queued"
    assert len(fake_transport.submitted) == 1


def test_webhook_exact_promotion_flag_defaults_to_fifo(
    monkeypatch, service, bucket, fake_transport
):
    from pipeline.scripts.ingest_hook.contract import load_manifest

    monkeypatch.delenv("INGEST_WEBHOOK_PROMOTE_EXACT", raising=False)
    older_path = write_submission(bucket, epoch="2026-05", rows=GOOD_ROWS[:3])
    newer_path = write_submission(bucket, epoch="2026-06")
    older = load_manifest(older_path)
    newer = load_manifest(newer_path)
    service.ledger.receive(
        older.epoch,
        older.category,
        older.manifest_sha,
        manifest_path=str(older_path.relative_to(bucket)),
        uploaded_by=older.uploaded_by,
    )

    result = service.receive_webhook(str(newer_path.relative_to(bucket)))

    assert result["job_name"] is not None
    assert older.manifest_sha[:8] in result["job_name"]
    assert service.ledger.status(
        older.epoch, older.category, older.manifest_sha
    ).status == "running"
    assert service.ledger.status(
        newer.epoch, newer.category, newer.manifest_sha
    ).status == "queued"
    assert len(fake_transport.submitted) == 1


# --------------------------------------------------------------------- G-4
def test_g4_sweep_catches_lost_webhook(sqlite_ledger, bucket, tmp_path):
    manifest_path = write_submission(bucket)  # no webhook ever fired

    result = sweep(sqlite_ledger, bucket, rehearsal_root=tmp_path / "staging")

    assert result["kicked"] == 1
    ran = [action for action in result["actions"] if action["action"] == "ran-inline"]
    assert ran and ran[0]["rc"] == 0
    from pipeline.scripts.ingest_hook.contract import load_manifest

    manifest = load_manifest(manifest_path)
    assert sqlite_ledger.status(manifest.epoch, manifest.category, manifest.manifest_sha).status == "complete"


def test_g4_sweep_is_noop_when_ledger_is_current(sqlite_ledger, bucket, tmp_path):
    write_submission(bucket)
    first = sweep(sqlite_ledger, bucket, rehearsal_root=tmp_path / "staging")
    second = sweep(sqlite_ledger, bucket, rehearsal_root=tmp_path / "staging")
    assert first["kicked"] == 1
    assert second["kicked"] == 0, "normal day = no-op watchdog"


def test_uploaded_by_flows_webhook_to_status_api(client, bucket):
    manifest_path = write_submission(bucket, uploaded_by="pl@jw.example")
    payload = _webhook(client, manifest_path, bucket).json()
    status = client.get(
        "/ingest/status",
        params={"epoch": payload["epoch"], "category": "ubist", "manifest_sha": payload["manifest_sha"]},
    ).json()
    assert status["uploaded_by"] == "pl@jw.example"
