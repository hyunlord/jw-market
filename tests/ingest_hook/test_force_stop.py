"""Exact-identity force-stop contract for active ingest Jobs."""
from __future__ import annotations

import io
import urllib.error

from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import job_launcher
from pipeline.scripts.ingest_hook.app import IngestService, create_app


EPOCH = "2026-07"
CATEGORY = "ubist"
MANIFEST_SHA = "a" * 64
RUN_ID = "20260725010101000000"


def _not_found(name: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=f"https://kubernetes.invalid/jobs/{name}",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


def _running_job(name: str) -> dict:
    return {
        "metadata": {
            "name": name,
            "uid": "job-uid-123",
            "resourceVersion": "456",
            "creationTimestamp": "2026-07-25T01:00:00Z",
        },
        "status": {"active": 1},
    }


def _seed_running(ledger) -> str:
    name = job_launcher.job_name(CATEGORY, MANIFEST_SHA, RUN_ID)
    ledger.receive(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        manifest_path="/input/demo-manifest.json",
        uploaded_by="demo@example.test",
    )
    ledger.mark_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        job_name=name,
        run_id=RUN_ID,
    )
    return name


class DeleteRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        return {"status": "Success"}


def test_force_stop_deletes_exact_job_then_reconciles_and_promotes(
    sqlite_ledger, fake_transport
):
    name = _seed_running(sqlite_ledger)
    queued = ("2026-08", CATEGORY, "b" * 64)
    sqlite_ledger.receive(*queued, manifest_path="/input/queued.json")
    inspected = 0

    def inspect(_namespace: str, requested_name: str) -> dict:
        nonlocal inspected
        assert requested_name == name
        inspected += 1
        if inspected == 1:
            return _running_job(name)
        raise _not_found(name)

    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=inspect,
        delete_transport=deleted,
        sleep=lambda _seconds: None,
        timestamp=lambda: "2026-07-25T01:02:03+00:00",
        now=lambda: "20260725010204000000",
    )

    result = service.force_stop(
        epoch=EPOCH,
        category=CATEGORY,
        manifest_sha=MANIFEST_SHA,
        run_id=RUN_ID,
        requested_by="pl@example.test",
    )

    assert result["status"] == "failed"
    assert result["job_name"] == name
    assert result["job_status"] == "Absent"
    assert result["promoted_job_name"] == sqlite_ledger.status(*queued).job_name
    failed = sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA)
    assert failed.status == "failed"
    assert "PL 강제 정지" in failed.reason
    assert "pl@example.test" in failed.reason
    assert sqlite_ledger.status(*queued).status == "running"
    assert len(fake_transport.submitted) == 1

    assert deleted.calls == [
        (
            f"/apis/batch/v1/namespaces/llmops/jobs/{name}",
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "gracePeriodSeconds": 0,
                "propagationPolicy": "Foreground",
                "preconditions": {
                    "uid": "job-uid-123",
                    "resourceVersion": "456",
                },
            },
        )
    ]
    transition = sqlite_ledger.status_transitions(
        EPOCH, CATEGORY, MANIFEST_SHA
    )[-1]
    assert transition.actor == "pl@example.test"
    assert transition.source == "manual_force_stop"
    assert transition.evidence["job_uid"] == "job-uid-123"
    assert transition.evidence["run_id"] == RUN_ID


def test_force_stop_publish_job_recovers_before_terminal_transition(
    sqlite_ledger, fake_transport
) -> None:
    _seed_running(sqlite_ledger)
    publish_name = "jw-ingest-publish-ubist-aaaaaaaa-publish-run"
    sqlite_ledger.mark_awaiting_approval(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        run_id=RUN_ID,
        candidate={"run_id": RUN_ID},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    assert sqlite_ledger.mark_publish_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        build_run_id=RUN_ID,
        publish_job_name=publish_name,
        approved_by="pl@example.test",
        approved_at="2026-08-04T01:00:00+00:00",
    )
    inspected = 0

    def inspect(_namespace: str, requested_name: str) -> dict:
        nonlocal inspected
        assert requested_name == publish_name
        inspected += 1
        if inspected == 1:
            return _running_job(publish_name)
        raise _not_found(publish_name)

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=inspect,
        delete_transport=DeleteRecorder(),
        sleep=lambda _seconds: None,
        timestamp=lambda: "2026-08-04T01:02:03+00:00",
    )
    recovered: list[str] = []
    service._recover_publish_activation = lambda entry: recovered.append(entry.job_name)

    result = service.force_stop(
        epoch=EPOCH,
        category=CATEGORY,
        manifest_sha=MANIFEST_SHA,
        run_id=RUN_ID,
        requested_by="pl@example.test",
    )

    assert recovered == [publish_name]
    assert result["job_name"] == publish_name
    assert sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA).status == "failed"


def test_force_stop_endpoint_rejects_run_mismatch_without_delete(sqlite_ledger):
    _seed_running(sqlite_ledger)
    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        inspect_transport=lambda _namespace, name: _running_job(name),
        delete_transport=deleted,
    )

    response = TestClient(create_app(service)).post(
        "/ingest/force-stop",
        json={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": "wrong-run",
            "requested_by": "pl@example.test",
        },
    )

    assert response.status_code == 409
    assert "run_id" in response.json()["detail"]
    assert deleted.calls == []
    assert sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA).status == "running"


def test_force_stop_requires_active_kubernetes_job(sqlite_ledger):
    name = _seed_running(sqlite_ledger)
    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        inspect_transport=lambda _namespace, _name: {
            **_running_job(name),
            "status": {
                "conditions": [
                    {"type": "Complete", "status": "True", "reason": "Complete"}
                ]
            },
        },
        delete_transport=deleted,
    )

    response = TestClient(create_app(service)).post(
        "/ingest/force-stop",
        json={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": RUN_ID,
            "requested_by": "pl@example.test",
        },
    )

    assert response.status_code == 409
    assert deleted.calls == []
    assert sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA).status == "running"


def test_force_stop_fails_closed_when_foreground_deletion_is_not_confirmed(
    sqlite_ledger
):
    name = _seed_running(sqlite_ledger)
    queued = ("2026-08", CATEGORY, "b" * 64)
    sqlite_ledger.receive(*queued, manifest_path="/input/queued.json")
    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        inspect_transport=lambda _namespace, _name: _running_job(name),
        delete_transport=deleted,
        sleep=lambda _seconds: None,
        deletion_attempts=2,
    )

    response = TestClient(create_app(service)).post(
        "/ingest/force-stop",
        json={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": RUN_ID,
            "requested_by": "pl@example.test",
        },
    )

    assert response.status_code == 503
    assert len(deleted.calls) == 1
    assert sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA).status == "running"
    assert sqlite_ledger.status(*queued).status == "queued"


def test_force_stop_does_not_touch_other_category(sqlite_ledger):
    name = _seed_running(sqlite_ledger)
    other = ("2026-07", "iqvia_nsa", "c" * 64)
    other_run = "20260725010202000000"
    other_name = job_launcher.job_name(other[1], other[2], other_run)
    sqlite_ledger.receive(*other, manifest_path="/input/other.json")
    sqlite_ledger.mark_running(*other, job_name=other_name, run_id=other_run)
    inspected = 0

    def inspect(_namespace: str, requested_name: str) -> dict:
        nonlocal inspected
        assert requested_name == name
        inspected += 1
        if inspected == 1:
            return _running_job(name)
        raise _not_found(name)

    service = IngestService(
        sqlite_ledger,
        None,
        inspect_transport=inspect,
        delete_transport=DeleteRecorder(),
        sleep=lambda _seconds: None,
    )
    service.force_stop(
        epoch=EPOCH,
        category=CATEGORY,
        manifest_sha=MANIFEST_SHA,
        run_id=RUN_ID,
        requested_by="pl@example.test",
    )

    assert sqlite_ledger.status(*other).status == "running"
    assert sqlite_ledger.status(*other).job_name == other_name


def test_targeted_reconciliation_rechecks_run_and_job_inside_transaction(
    sqlite_ledger
):
    name = _seed_running(sqlite_ledger)

    changed = sqlite_ledger.reconcile_terminal(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        status="failed",
        reason="must not apply",
        actor="operator@example.com",
        source="manual_force_stop",
        evidence={},
        expected_job_name=name,
        expected_run_id="different-run",
    )

    assert changed is False
    assert sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA).status == "running"
    assert len(sqlite_ledger.status_transitions(EPOCH, CATEGORY, MANIFEST_SHA)) == 2
