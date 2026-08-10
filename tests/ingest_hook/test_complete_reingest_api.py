from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from pipeline.scripts.ingest_hook import config, job_runner
from pipeline.scripts.ingest_hook.app import IngestService, create_app


IDENTITY = ("2026-06", "ubist", "a" * 64)
REQUEST_ID = "b6a8e00f-7717-4697-9230-e45192d5d7d2"


def _complete_identity(sqlite_ledger, identity=IDENTITY) -> None:
    sqlite_ledger.receive(
        *identity,
        manifest_path=f"_manifests/{identity[1]}/{identity[0]}/manifest.json",
        uploaded_by="original@jw.example",
    )
    assert sqlite_ledger.mark_running(
        *identity,
        job_name=f"jw-ingest-{identity[1]}-original",
        run_id="20260809010101000000",
    )
    sqlite_ledger.mark_complete(*identity, row_counts={f"epoch:{identity[0]}": 137_836})


def _payload(**overrides) -> dict:
    payload = {
        "epoch": IDENTITY[0],
        "category": IDENTITY[1],
        "manifest_sha": IDENTITY[2],
        "request_id": REQUEST_ID,
        "mode": "mart_from_existing_raw",
        "requested_by": "operator@jw.example",
        "reason": "MI Master definition changed",
    }
    payload.update(overrides)
    return payload


def test_complete_reingest_api_submits_append_only_attempt(
    sqlite_ledger, fake_transport
) -> None:
    _complete_identity(sqlite_ledger)
    before = asdict(sqlite_ledger.status(*IDENTITY))
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260809221530123456",
    )
    client = TestClient(create_app(service))

    response = client.post("/ingest/reingest", json=_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["action"] == "submitted"
    assert body["created"] is True
    assert body["request_id"] == REQUEST_ID
    assert body["run_id"] == "20260809221530123456"
    assert body["affected_scope"] == {
        "dimension": "source",
        "count": 1,
        "values": ["ubist"],
    }
    assert asdict(sqlite_ledger.status(*IDENTITY)) == before

    assert len(fake_transport.submitted) == 1
    _path, job = fake_transport.submitted[0]
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert job["metadata"]["labels"]["app"] == "jw-complete-reingest"
    assert container["command"][:3] == [
        "python",
        "-m",
        "pipeline.scripts.ingest_hook.stage_log_runner",
    ]
    assert "--runner" not in container["command"]
    assert container["name"] == "ingest"

    attempt_events = [
        event
        for event in sqlite_ledger.stage_events(*IDENTITY)
        if event.run_id == body["run_id"]
    ]
    assert [(event.stage, event.status) for event in attempt_events] == [
        ("job_submit", "complete"),
    ]

    history = client.get("/ingest/history", params={"limit": 100}).json()
    attempt = next(item for item in history["items"] if item["run_id"] == body["run_id"])
    assert attempt["ledger"] is None
    assert attempt["reingest"] == {
        "request_id": REQUEST_ID,
        "mode": "mart_from_existing_raw",
        "requested_by": "operator@jw.example",
        "reason": "MI Master definition changed",
        "affected_scope": {
            "dimension": "source",
            "count": 1,
            "values": ["ubist"],
        },
        "code_revision": None,
        "image_digest": "sha256:" + config.DEFAULT_JOB_IMAGE.rsplit("@sha256:", 1)[1],
        "status": "running",
        "terminal_reason": None,
        "job_name": body["job_name"],
    }


def test_normal_runner_adapter_preserves_completed_parent(sqlite_ledger, fake_transport) -> None:
    _complete_identity(sqlite_ledger)
    parent_before = asdict(sqlite_ledger.status(*IDENTITY))
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260809221530123456",
    )
    response = TestClient(create_app(service)).post("/ingest/reingest", json=_payload())
    assert response.status_code == 202

    attempt_ledger = job_runner._ledger_for_run(
        sqlite_ledger,
        IDENTITY,
        response.json()["run_id"],
    )
    assert attempt_ledger.status(*IDENTITY).status == "running"
    attempt_ledger.mark_complete(*IDENTITY, row_counts={"epoch:2026-06": 137_836})

    assert asdict(sqlite_ledger.status(*IDENTITY)) == parent_before
    attempt = sqlite_ledger.complete_reingest_attempts()[0]
    assert attempt.status == "complete"


def test_reingest_publish_lifecycle_cas_uses_attempt_without_mutating_parent(
    sqlite_ledger,
    fake_transport,
) -> None:
    _complete_identity(sqlite_ledger)
    parent_before = asdict(sqlite_ledger.status(*IDENTITY))
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260809221530123456",
    )
    response = TestClient(create_app(service)).post("/ingest/reingest", json=_payload())
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    attempt_ledger = job_runner._ledger_for_run(sqlite_ledger, IDENTITY, run_id)
    prepared_at = datetime.now(timezone.utc)

    attempt_ledger.mark_awaiting_approval(
        *IDENTITY,
        run_id=run_id,
        candidate={"automatic_publish": {"hard_gates": {}}},
        prepared_at=prepared_at.isoformat(),
        expires_at=(prepared_at + timedelta(minutes=30)).isoformat(),
    )

    assert asdict(sqlite_ledger.status(*IDENTITY)) == parent_before
    assert attempt_ledger.status(*IDENTITY).status == "awaiting_approval"
    approval = TestClient(create_app(service)).post(
        "/ingest/publish/approve",
        json={
            "epoch": IDENTITY[0],
            "category": IDENTITY[1],
            "manifest_sha": IDENTITY[2],
            "run_id": run_id,
            "requested_by": "operator@jw.example",
        },
    )
    assert approval.status_code == 200
    publish_job = approval.json()["publish_job_name"]
    assert asdict(sqlite_ledger.status(*IDENTITY)) == parent_before
    publish_view = job_runner._ledger_for_run(sqlite_ledger, IDENTITY, run_id)
    assert publish_view.status(*IDENTITY).status == "publish_running"
    assert publish_view.status(*IDENTITY).job_name == publish_job

    publish_view.mark_complete(*IDENTITY, row_counts={"rows": 1})

    assert asdict(sqlite_ledger.status(*IDENTITY)) == parent_before
    assert sqlite_ledger.complete_reingest_attempts()[0].status == "complete"


def test_complete_reingest_api_same_uuid_is_idempotent(
    sqlite_ledger, fake_transport
) -> None:
    _complete_identity(sqlite_ledger)
    run_ids = iter(("20260809221530123456", "20260809221530123457"))
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: next(run_ids),
            )
        )
    )

    first = client.post("/ingest/reingest", json=_payload())
    transition_count = len(sqlite_ledger.status_transitions(*IDENTITY))
    second = client.post("/ingest/reingest", json=_payload())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["created"] is False
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(sqlite_ledger.status_transitions(*IDENTITY)) == transition_count


def test_complete_reingest_api_rejects_nonstandard_attempt_run_id(
    sqlite_ledger, fake_transport
) -> None:
    _complete_identity(sqlite_ledger)
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: "uuid-shaped-run-id",
            )
        )
    )

    with pytest.raises(RuntimeError, match="exactly 20 digits"):
        client.post("/ingest/reingest", json=_payload())

    assert fake_transport.submitted == []


def test_complete_reingest_api_accepts_noncomplete_parent_without_mutating_it(
    sqlite_ledger, fake_transport
) -> None:
    sqlite_ledger.receive(
        *IDENTITY,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
    )
    before = asdict(sqlite_ledger.status(*IDENTITY))
    response = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    ).post("/ingest/reingest", json=_payload())

    assert response.status_code == 202
    assert asdict(sqlite_ledger.status(*IDENTITY)) == before
    assert len(fake_transport.submitted) == 1
    attempt_ledger = job_runner._ledger_for_run(
        sqlite_ledger, IDENTITY, response.json()["run_id"]
    )
    assert attempt_ledger.status(*IDENTITY).status == "running"


def test_complete_reingest_api_supports_csd_keyword(
    sqlite_ledger, fake_transport
) -> None:
    identity = ("2026-05", "iqvia_csd_keyword", "c" * 64)
    _complete_identity(sqlite_ledger, identity)
    response = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    ).post(
        "/ingest/reingest",
        json=_payload(
            epoch=identity[0],
            category=identity[1],
            manifest_sha=identity[2],
        ),
    )

    assert response.status_code == 202
    assert response.json()["action"] == "submitted"
    assert len(fake_transport.submitted) == 1


def test_complete_reingest_waits_behind_active_upload_instead_of_rejecting(
    sqlite_ledger, fake_transport
) -> None:
    _complete_identity(sqlite_ledger)
    active = ("2026-Q1", "iqvia_nsa", "d" * 64)
    sqlite_ledger.receive(*active, manifest_path="/input/nsa.json")
    sqlite_ledger.mark_running(*active, job_name="nsa-active", run_id="active-run")
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                inspect_transport=lambda _namespace, _name: {
                    "metadata": {"name": "nsa-active"},
                    "status": {"active": 1},
                },
            )
        )
    )

    response = client.post("/ingest/reingest", json=_payload())

    assert response.status_code == 202
    assert response.json()["action"] == "pending"
    assert response.json()["queue_position"] == 1
    assert fake_transport.submitted == []


def test_complete_reingest_terminal_promotes_next_source(
    sqlite_ledger, fake_transport
) -> None:
    keyword_identity = ("2026-05", "iqvia_csd_keyword", "e" * 64)
    _complete_identity(sqlite_ledger)
    _complete_identity(sqlite_ledger, keyword_identity)
    run_ids = iter(("20260810010101000000", "20260810010102000000"))
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: next(run_ids),
            )
        )
    )
    first = client.post("/ingest/reingest", json=_payload())
    second_request_id = "d985181b-e8ab-4910-9138-4203f3054d1d"
    second = client.post(
        "/ingest/reingest",
        json=_payload(
            epoch=keyword_identity[0],
            category=keyword_identity[1],
            manifest_sha=keyword_identity[2],
            request_id=second_request_id,
        ),
    )

    assert first.json()["action"] == "submitted"
    assert second.json()["action"] == "pending"
    terminal = client.post(
        "/ingest/reingest/terminal",
        json={
            "epoch": IDENTITY[0],
            "category": IDENTITY[1],
            "manifest_sha": IDENTITY[2],
            "request_id": REQUEST_ID,
            "run_id": first.json()["run_id"],
            "status": "failed",
            "reason": "injected failure",
            "job_name": first.json()["job_name"],
        },
    )

    assert terminal.status_code == 200
    assert terminal.json()["promoted_job_name"] == second.json()["job_name"]
    attempts = {item.request_id: item for item in sqlite_ledger.complete_reingest_attempts()}
    assert attempts[REQUEST_ID].status == "failed"
    assert attempts[second_request_id].status == "running"
    assert len(fake_transport.submitted) == 2


def test_force_stop_cancels_pending_complete_reingest_without_job_delete(
    sqlite_ledger, fake_transport
) -> None:
    keyword_identity = ("2026-05", "iqvia_csd_keyword", "f" * 64)
    _complete_identity(sqlite_ledger)
    _complete_identity(sqlite_ledger, keyword_identity)
    run_ids = iter(("20260810010201000000", "20260810010202000000"))
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: next(run_ids),
            )
        )
    )
    client.post("/ingest/reingest", json=_payload())
    request_id = "0bc3c41a-ff14-4d7e-ad87-0758fd49fca2"
    pending = client.post(
        "/ingest/reingest",
        json=_payload(
            epoch=keyword_identity[0],
            category=keyword_identity[1],
            manifest_sha=keyword_identity[2],
            request_id=request_id,
        ),
    ).json()

    stopped = client.post(
        "/ingest/force-stop",
        json={
            "epoch": keyword_identity[0],
            "category": keyword_identity[1],
            "manifest_sha": keyword_identity[2],
            "run_id": pending["run_id"],
            "requested_by": "operator@jw.example",
        },
    )

    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert stopped.json()["job_name"] is None
    assert len(fake_transport.submitted) == 1
    attempt = next(
        item
        for item in sqlite_ledger.complete_reingest_attempts()
        if item.request_id == request_id
    )
    assert attempt.status == "cancelled"


def test_complete_reingest_api_records_terminal_failure_when_job_submit_fails(
    sqlite_ledger,
) -> None:
    _complete_identity(sqlite_ledger)

    def fail_transport(_path: str, _body: dict) -> dict:
        raise RuntimeError("synthetic submit failure")

    client = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fail_transport))
    )

    with pytest.raises(RuntimeError, match="synthetic submit failure"):
        client.post("/ingest/reingest", json=_payload())

    terminal = sqlite_ledger.status_transitions(*IDENTITY)[-1]
    assert terminal.source == "complete_reingest_terminal"
    assert terminal.status == "failed"
    assert terminal.evidence["request_id"] == REQUEST_ID
    assert asdict(sqlite_ledger.status(*IDENTITY))["status"] == "complete"
