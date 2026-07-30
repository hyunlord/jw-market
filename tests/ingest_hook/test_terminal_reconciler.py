"""Terminal Kubernetes Job reconciliation for stale running ledger rows."""
from __future__ import annotations

import io
import urllib.error

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import job_launcher
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.sweep import sweep


def _job_payload(status: str, *, reason: str | None = None) -> dict:
    condition = {
        "type": status,
        "status": "True",
        "reason": reason or status,
        "message": f"injected {status.lower()} condition",
        "lastTransitionTime": "2026-07-24T00:00:00Z",
    }
    return {
        "metadata": {"name": "jw-ingest-ubist-stale", "creationTimestamp": "2026-07-23T00:00:00Z"},
        "status": {"conditions": [condition]},
    }


def _seed_running_and_queued(ledger) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    running = ("2026-06", "ubist", "a" * 64)
    queued = ("2026-07", "ubist", "b" * 64)
    ledger.receive(*running, manifest_path="/input/old.json")
    ledger.mark_running(*running, job_name="jw-ingest-ubist-stale", run_id="run-old")
    ledger.receive(*queued, manifest_path="/input/new.json")
    return running, queued


def test_active_job_is_not_touched(sqlite_ledger, fake_transport):
    running, queued = _seed_running_and_queued(sqlite_ledger)

    def inspect(_namespace: str, _name: str) -> dict:
        return {"metadata": {"name": "jw-ingest-ubist-stale"}, "status": {"active": 1}}

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=inspect,
    )
    result = service.reconcile_terminal_jobs()

    assert result["reconciled"] == 0
    assert result["actions"][0]["job_status"] == "Running"
    assert sqlite_ledger.status(*running).status == "running"
    assert sqlite_ledger.status(*queued).status == "queued"
    assert fake_transport.submitted == []


def test_deadline_exceeded_marks_failed_and_promotes_next(sqlite_ledger, fake_transport):
    running, queued = _seed_running_and_queued(sqlite_ledger)

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, _name: _job_payload(
            "Failed", reason="DeadlineExceeded"
        ),
        now=lambda: "20260724010101000000",
    )
    result = service.reconcile_terminal_jobs()

    failed = sqlite_ledger.status(*running)
    promoted = sqlite_ledger.status(*queued)
    assert result["reconciled"] == 1
    assert failed.status == "failed"
    assert "terminal-present" in failed.reason
    assert "DeadlineExceeded" in failed.reason
    assert promoted.status == "running"
    assert result["actions"][0]["promoted_job_name"] == promoted.job_name
    assert len(fake_transport.submitted) == 1

    history = sqlite_ledger.status_transitions(*running)
    terminal = history[-1]
    assert terminal.previous_status == "running"
    assert terminal.status == "failed"
    assert terminal.actor == "terminal_job_reconciler"
    assert terminal.source == "kubernetes_job_terminal_present"
    assert terminal.evidence["conditions"][0]["reason"] == "DeadlineExceeded"


def test_complete_job_marks_complete_and_promotes_next(sqlite_ledger, fake_transport):
    running, queued = _seed_running_and_queued(sqlite_ledger)
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, _name: _job_payload("Complete"),
        now=lambda: "20260724020202000000",
    )

    result = service.reconcile_terminal_jobs()

    assert sqlite_ledger.status(*running).status == "complete"
    assert sqlite_ledger.status(*queued).status == "running"
    assert result["actions"][0]["ledger_status"] == "complete"


def test_absent_job_is_distinct_from_terminal_present(sqlite_ledger, fake_transport):
    running, queued = _seed_running_and_queued(sqlite_ledger)

    def missing(_namespace: str, name: str) -> dict:
        raise urllib.error.HTTPError(
            url=f"https://kubernetes.invalid/jobs/{name}",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=missing,
        now=lambda: "20260724030303000000",
    )
    service.reconcile_terminal_jobs()

    failed = sqlite_ledger.status(*running)
    terminal = sqlite_ledger.status_transitions(*running)[-1]
    assert failed.status == "failed"
    assert "job-absent" in failed.reason
    assert terminal.source == "kubernetes_job_absent"
    assert terminal.evidence["job_status"] == "Absent"
    assert sqlite_ledger.status(*queued).status == "running"


def test_inspection_error_fails_closed_without_ledger_change(sqlite_ledger, fake_transport):
    running, queued = _seed_running_and_queued(sqlite_ledger)

    def unavailable(_namespace: str, _name: str) -> dict:
        raise TimeoutError("injected api timeout")

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=unavailable,
    )
    result = service.reconcile_terminal_jobs()

    assert result["reconciled"] == 0
    assert result["inspection_failures"] == 1
    assert sqlite_ledger.status(*running).status == "running"
    assert sqlite_ledger.status(*queued).status == "queued"
    assert sqlite_ledger.status_transitions(*running)[-1].status == "running"


def test_promotion_reconciles_terminal_blocker_before_claim(
    sqlite_ledger, fake_transport
):
    running, queued = _seed_running_and_queued(sqlite_ledger)
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, _name: _job_payload(
            "Failed", reason="BackoffLimitExceeded"
        ),
        now=lambda: "20260730050505000000",
    )

    promoted_job_name = service.promote("ubist")

    assert promoted_job_name == sqlite_ledger.status(*queued).job_name
    assert sqlite_ledger.status(*running).status == "failed"
    assert sqlite_ledger.status(*queued).status == "running"
    assert len(fake_transport.submitted) == 1


def test_promotion_leaves_live_blocker_untouched(sqlite_ledger, fake_transport):
    running, queued = _seed_running_and_queued(sqlite_ledger)
    inspected: list[str] = []

    def inspect(_namespace: str, name: str) -> dict:
        inspected.append(name)
        return {"metadata": {"name": name}, "status": {"active": 1}}

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=inspect,
    )

    assert service.promote("ubist") is None
    assert inspected == ["jw-ingest-ubist-stale"]
    assert sqlite_ledger.status(*running).status == "running"
    assert sqlite_ledger.status(*queued).status == "queued"
    assert fake_transport.submitted == []


def test_promotion_inspection_failure_is_explicit_and_fail_closed(
    sqlite_ledger, fake_transport
):
    running, queued = _seed_running_and_queued(sqlite_ledger)

    def unavailable(_namespace: str, _name: str) -> dict:
        raise TimeoutError("injected api timeout")

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=unavailable,
    )

    with pytest.raises(RuntimeError, match="terminal reconciliation inspection failed"):
        service.promote("ubist")

    assert sqlite_ledger.status(*running).status == "running"
    assert sqlite_ledger.status(*queued).status == "queued"
    assert fake_transport.submitted == []


def test_stale_terminal_observation_cannot_fail_a_replacement_run(
    sqlite_ledger, fake_transport
):
    running = ("2026-06", "ubist", "a" * 64)
    sqlite_ledger.receive(*running, manifest_path="/input/old.json")
    sqlite_ledger.mark_running(
        *running,
        job_name="jw-ingest-ubist-stale",
        run_id="run-old",
    )

    def inspect(_namespace: str, _name: str) -> dict:
        assert sqlite_ledger.reconcile_terminal(
            *running,
            status="failed",
            reason="injected concurrent terminal callback",
            actor="test",
            source="test",
            evidence={},
            expected_job_name="jw-ingest-ubist-stale",
            expected_run_id="run-old",
        )
        sqlite_ledger.receive(*running, manifest_path="/input/old.json")
        sqlite_ledger.mark_running(
            *running,
            job_name="jw-ingest-ubist-replacement",
            run_id="run-replacement",
        )
        return _job_payload("Failed", reason="BackoffLimitExceeded")

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=inspect,
    )

    result = service.reconcile_terminal_jobs()

    replacement = sqlite_ledger.status(*running)
    assert replacement.status == "running"
    assert replacement.job_name == "jw-ingest-ubist-replacement"
    assert replacement.run_id == "run-replacement"
    assert result["actions"][-1]["action"] == "state-changed-concurrently"


def test_transition_history_is_append_only(sqlite_ledger):
    identity = ("2026-07", "ubist", "c" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-history",
        run_id="run-history",
    )
    sqlite_ledger.reconcile_terminal(
        *identity,
        status="failed",
        reason="terminal-present: DeadlineExceeded",
        actor="terminal_job_reconciler",
        source="kubernetes_job_terminal_present",
        evidence={"job_status": "Failed"},
    )

    history = sqlite_ledger.status_transitions(*identity)
    assert [(item.previous_status, item.status) for item in history] == [
        (None, "queued"),
        ("queued", "running"),
        ("running", "failed"),
    ]
    assert len({item.event_id for item in history}) == 3


def test_reconcile_endpoint_repairs_terminal_before_queue_scan(
    sqlite_ledger, fake_transport
):
    running, queued = _seed_running_and_queued(sqlite_ledger)
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, _name: _job_payload(
            "Failed", reason="DeadlineExceeded"
        ),
        now=lambda: "20260724040404000000",
    )

    response = TestClient(create_app(service)).post("/ingest/reconcile")

    assert response.status_code == 200
    assert response.json()["terminal"]["reconciled"] == 1
    assert sqlite_ledger.status(*running).status == "failed"
    assert sqlite_ledger.status(*queued).status == "running"


def test_sweep_reconciles_terminal_job_before_manifest_scan(
    sqlite_ledger, fake_transport, tmp_path, monkeypatch
):
    running, queued = _seed_running_and_queued(sqlite_ledger)
    monkeypatch.setattr(
        job_launcher,
        "inspect_job",
        lambda _name, transport=None: job_launcher.JobObservation(
            status="Failed",
            reason="DeadlineExceeded",
            evidence={
                "job_name": "jw-ingest-ubist-stale",
                "job_status": "Failed",
                "conditions": [{"reason": "DeadlineExceeded"}],
            },
        ),
    )

    result = sweep(sqlite_ledger, tmp_path, transport=fake_transport)

    assert result["found"] == 0
    assert result["terminal"]["reconciled"] == 1
    assert sqlite_ledger.status(*running).status == "failed"
    assert sqlite_ledger.status(*queued).status == "running"
