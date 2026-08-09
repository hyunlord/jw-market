from __future__ import annotations

from dataclasses import asdict

from fastapi.testclient import TestClient
import pytest

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.app import IngestService, create_app


IDENTITY = ("2026-06", "ubist", "a" * 64)
REQUEST_ID = "b6a8e00f-7717-4697-9230-e45192d5d7d2"


def _complete_identity(sqlite_ledger) -> None:
    sqlite_ledger.receive(
        *IDENTITY,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
        uploaded_by="original@jw.example",
    )
    assert sqlite_ledger.mark_running(
        *IDENTITY,
        job_name="jw-ingest-ubist-original",
        run_id="20260809010101000000",
    )
    sqlite_ledger.mark_complete(*IDENTITY, row_counts={"epoch:2026-06": 137_836})


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
        "pipeline.scripts.ingest_hook.complete_reingest_runner",
    ]
    assert "--request-id" in container["command"]
    assert "--affected-scope-json" in container["command"]

    attempt_events = [
        event
        for event in sqlite_ledger.stage_events(*IDENTITY)
        if event.run_id == body["run_id"]
    ]
    assert [(event.stage, event.status) for event in attempt_events] == [
        ("job_submit", "complete"),
        ("request_validate", "running"),
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
    second = client.post("/ingest/reingest", json=_payload())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["created"] is False
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(sqlite_ledger.status_transitions(*IDENTITY)) == 4


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


def test_complete_reingest_api_rejects_noncomplete_parent(
    sqlite_ledger, fake_transport
) -> None:
    sqlite_ledger.receive(
        *IDENTITY,
        manifest_path="_manifests/ubist/2026-06/manifest.json",
    )
    response = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    ).post("/ingest/reingest", json=_payload())

    assert response.status_code == 409
    assert "must be complete" in response.json()["detail"]
    assert fake_transport.submitted == []


def test_complete_reingest_api_rejects_non_numeric_category(
    sqlite_ledger, fake_transport
) -> None:
    response = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    ).post(
        "/ingest/reingest",
        json=_payload(category="iqvia_csd_keyword"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "complete reingest supports ubist and iqvia_nsa only"
    assert fake_transport.submitted == []


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
