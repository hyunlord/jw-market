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

    assert result["status"] == "cancelled"
    assert result["job_name"] == name
    assert result["job_status"] == "Absent"
    assert result["promoted_job_name"] == sqlite_ledger.status(*queued).job_name
    cancelled = sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA)
    assert cancelled.status == "cancelled"
    assert "사용자 중단" in cancelled.reason
    assert "pl@example.test" in cancelled.reason
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


def test_force_stop_rejects_after_publish_boundary_without_deleting_job(
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
    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, _name: _running_job(publish_name),
        delete_transport=deleted,
        sleep=lambda _seconds: None,
        timestamp=lambda: "2026-08-04T01:02:03+00:00",
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
    assert "publish" in response.json()["detail"]
    assert deleted.calls == []
    assert sqlite_ledger.status(EPOCH, CATEGORY, MANIFEST_SHA).status == "publish_running"


def test_force_stop_rejects_complete_reingest_after_publish_boundary(
    sqlite_ledger,
) -> None:
    request_id = "be244068-6c0a-455b-9f70-cbc0bd437dc7"
    run_id = "20260810020304000000"
    name = job_launcher.complete_reingest_job_name(CATEGORY, MANIFEST_SHA, run_id)
    sqlite_ledger.receive(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        manifest_path="/input/demo-manifest.json",
    )
    sqlite_ledger.mark_running(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        job_name="jw-ingest-ubist-original",
        run_id="20260809010101000000",
    )
    sqlite_ledger.mark_complete(EPOCH, CATEGORY, MANIFEST_SHA, row_counts={})
    sqlite_ledger.record_complete_reingest_request(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        request_id=request_id,
        run_id=run_id,
        mode="mart_from_existing_raw",
        requested_by="operator@example.test",
        reason="logic changed",
        affected_scope={"dimension": "source", "count": 1, "values": [CATEGORY]},
    )
    assert sqlite_ledger.record_complete_reingest_started(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        request_id=request_id,
        run_id=run_id,
        job_name=name,
    )
    sqlite_ledger.record_stage(
        EPOCH,
        CATEGORY,
        MANIFEST_SHA,
        run_id=run_id,
        seq=5,
        stage="mart_publish",
        status="running",
    )
    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        inspect_transport=lambda _namespace, _name: _running_job(name),
        delete_transport=deleted,
    )

    response = TestClient(create_app(service)).post(
        "/ingest/force-stop",
        json={
            "epoch": EPOCH,
            "category": CATEGORY,
            "manifest_sha": MANIFEST_SHA,
            "run_id": run_id,
            "requested_by": "pl@example.test",
        },
    )

    assert response.status_code == 409
    assert "publish" in response.json()["detail"]
    assert deleted.calls == []
    attempt = sqlite_ledger.complete_reingest_attempts(category=CATEGORY)[0]
    assert attempt.status == "running"


def test_force_stop_cancels_pending_without_touching_kubernetes(
    sqlite_ledger, fake_transport
):
    active_name = _seed_running(sqlite_ledger)
    pending = ("2026-Q1", "iqvia_nsa", "d" * 64)
    sqlite_ledger.receive(*pending, manifest_path="/input/nsa.json")
    deleted = DeleteRecorder()
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, _name: _running_job(active_name),
        delete_transport=deleted,
        timestamp=lambda: "2026-08-10T01:02:03+00:00",
    )

    result = service.force_stop(
        epoch=pending[0],
        category=pending[1],
        manifest_sha=pending[2],
        run_id=None,
        requested_by="pl@example.test",
    )

    assert result["status"] == "cancelled"
    assert result["job_name"] is None
    assert result["promoted_job_name"] is None
    assert sqlite_ledger.status(*pending).status == "cancelled"
    assert deleted.calls == []


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
