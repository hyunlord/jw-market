"""Queue observability and event-driven category drain contracts."""
from __future__ import annotations

import threading
import urllib.error

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.completion_signal import PublishResult
from pipeline.scripts.ingest_hook.job_launcher import render_job


def _seed(
    ledger,
    *,
    epoch: str,
    category: str,
    manifest_sha: str,
) -> tuple[str, str, str]:
    identity = (epoch, category, manifest_sha)
    ledger.receive(*identity, manifest_path=f"/input/{manifest_sha[:8]}.json")
    return identity


def test_queue_list_exposes_exact_webhook_entries_without_portal_store(
    sqlite_ledger,
) -> None:
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="a" * 64,
    )
    running = _seed(
        sqlite_ledger,
        epoch="2026-07",
        category="iqvia_nsa",
        manifest_sha="b" * 64,
    )
    sqlite_ledger.mark_running(
        *running,
        job_name="jw-ingest-iqvia-nsa-bbbbbbbb-run",
        run_id="run",
    )

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/queue"
    )

    assert response.status_code == 200
    entries = response.json()["items"]
    assert set(entries[0]) == {
        "epoch",
        "category",
        "manifest_sha",
        "status",
        "reason",
        "job_name",
        "run_id",
        "uploaded_by",
        "received_at",
        "started_at",
        "finished_at",
        "blocked_by_category",
        "blocked_by_global",
        "kind",
        "queue_position",
        "request_id",
        "requires_reconcile",
    }
    assert [
        (entry["epoch"], entry["category"], entry["manifest_sha"], entry["status"])
        for entry in entries
    ] == [
        (*running, "running"),
        (*queued, "queued"),
    ]


def test_queue_list_for_unknown_category_is_an_empty_additive_response(
    sqlite_ledger,
) -> None:
    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/queue",
        params={"category": "not-configured"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_queue_omits_non_portal_category_rows(sqlite_ledger) -> None:
    _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="unexpected",
        manifest_sha="c" * 64,
    )
    allowed = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="iqvia_nsa",
        manifest_sha="d" * 64,
    )

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/queue"
    )

    assert response.status_code == 200
    assert [
        (item["epoch"], item["category"], item["manifest_sha"])
        for item in response.json()["items"]
    ] == [allowed]


def test_queued_status_distinguishes_category_blocker(sqlite_ledger) -> None:
    running = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *running,
        job_name="jw-ingest-ubist-aaaaaaaa-run",
        run_id="run",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/status",
        params={
            "epoch": queued[0],
            "category": queued[1],
            "manifest_sha": queued[2],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked_by_category"] is True
    assert payload["requires_reconcile"] is False


def test_queued_status_distinguishes_global_blocker_from_other_category(
    sqlite_ledger,
) -> None:
    running = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *running,
        job_name="jw-ingest-ubist-aaaaaaaa-run",
        run_id="run",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-Q1",
        category="iqvia_nsa",
        manifest_sha="b" * 64,
    )
    client = TestClient(create_app(IngestService(sqlite_ledger, None)))

    status = client.get(
        "/ingest/status",
        params={
            "epoch": queued[0],
            "category": queued[1],
            "manifest_sha": queued[2],
        },
    ).json()
    queue_item = next(
        item
        for item in client.get("/ingest/queue").json()["items"]
        if item["manifest_sha"] == queued[2]
    )

    assert status["blocked_by_global"] is True
    assert status["blocked_by_category"] is False
    assert status["requires_reconcile"] is False
    assert status["category_blocker"] is None
    assert status["global_blocker"] == {
        "epoch": running[0],
        "category": running[1],
        "manifest_sha": running[2],
        "run_id": "run",
        "job_name": "jw-ingest-ubist-aaaaaaaa-run",
    }
    assert queue_item["blocked_by_global"] is True
    assert queue_item["blocked_by_category"] is False
    assert queue_item["requires_reconcile"] is False


def test_unblocked_queued_status_requires_reconcile(sqlite_ledger) -> None:
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/status",
        params={
            "epoch": queued[0],
            "category": queued[1],
            "manifest_sha": queued[2],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked_by_category"] is False
    assert payload["requires_reconcile"] is True
    assert payload["category_blocker"] is None


def test_blocked_status_exposes_the_exact_running_identity(sqlite_ledger) -> None:
    blocker = ("2026-05", "ubist", "a" * 64)
    blocked = ("2026-06", "ubist", "b" * 64)
    sqlite_ledger.receive(*blocker, manifest_path="/input/blocker.json")
    sqlite_ledger.mark_running(
        *blocker,
        job_name="jw-ingest-ubist-blocker",
        run_id="run-blocker",
    )
    sqlite_ledger.receive(*blocked, manifest_path="/input/blocked.json")
    client = TestClient(create_app(IngestService(sqlite_ledger, None)))

    response = client.get(
        "/ingest/status",
        params={
            "epoch": blocked[0],
            "category": blocked[1],
            "manifest_sha": blocked[2],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["blocked_by_category"] is True
    assert payload["category_blocker"] == {
        "epoch": "2026-05",
        "manifest_sha": "a" * 64,
        "run_id": "run-blocker",
        "job_name": "jw-ingest-ubist-blocker",
    }


def test_status_preserves_existing_keys_and_adds_only_queue_flags(
    sqlite_ledger,
) -> None:
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )

    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/status",
        params={
            "epoch": queued[0],
            "category": queued[1],
            "manifest_sha": queued[2],
        },
    )

    assert response.status_code == 200
    assert set(response.json()) == {
        "epoch",
        "category",
        "manifest_sha",
        "status",
        "reason",
        "job_name",
        "uploaded_by",
        "received_at",
        "finished_at",
        "row_counts",
        "inventory_run_id",
        "file_count",
        "classified_file_count",
        "inventory_file_counts",
        "manifest_file_count",
        "inventory_file_count",
        "execution_period_from",
        "execution_period_to",
        "current_stage",
        "stages",
        "signals",
        "log_ref",
        "blocked_by_category",
        "blocked_by_global",
        "requires_reconcile",
        "category_blocker",
        "global_blocker",
        "queue_position",
        "expected_stages",
        "prepared",
    }


def test_status_exposes_awaiting_approval_prepared_signal(sqlite_ledger) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-bbbbbbbb-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "activation_journal": "/market-output/.ubist_activation_build_run.json",
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )

    response = TestClient(create_app(IngestService(
        sqlite_ledger,
        None,
        timestamp=lambda: "2026-08-04T01:00:00+00:00",
    ))).get(
        "/ingest/status",
        params={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "awaiting_approval"
    assert payload["blocked_by_category"] is True
    assert payload["requires_reconcile"] is False
    assert payload["prepared"] == {
        "run_id": "build-run",
        "prepared_at": "2026-08-04T00:00:00+00:00",
        "expires_at": "2026-08-05T00:00:00+00:00",
        "expired": False,
        "publish_job_name": None,
    }


def test_queue_list_marks_approval_and_publish_states_as_category_blockers(
    sqlite_ledger,
) -> None:
    blocker = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *blocker,
        job_name="jw-ingest-ubist-aaaaaaaa-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *blocker,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    client = TestClient(create_app(IngestService(sqlite_ledger, None)))

    awaiting = client.get("/ingest/queue").json()["items"]
    queued_item = next(item for item in awaiting if item["manifest_sha"] == queued[2])
    assert queued_item["blocked_by_category"] is True
    assert queued_item["requires_reconcile"] is False

    assert sqlite_ledger.mark_publish_running(
        *blocker,
        build_run_id="build-run",
        publish_job_name="jw-ingest-publish-ubist-aaaaaaaa-run",
        approved_by="pl@example.com",
        approved_at="2026-08-04T01:00:00+00:00",
    )
    publishing = client.get("/ingest/queue").json()["items"]
    queued_item = next(item for item in publishing if item["manifest_sha"] == queued[2])
    assert queued_item["blocked_by_category"] is True
    assert queued_item["requires_reconcile"] is False


def test_publish_approval_submits_publish_job_idempotently(sqlite_ledger, fake_transport) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-bbbbbbbb-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "activation_journal": "/market-output/.ubist_activation_build_run.json",
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "publish-run",
        timestamp=lambda: "2026-08-04T01:00:00+00:00",
    )
    client = TestClient(create_app(service))

    payload = {
        "epoch": identity[0],
        "category": identity[1],
        "manifest_sha": identity[2],
        "run_id": "build-run",
        "requested_by": "pl@example.com",
    }
    first = client.post("/ingest/publish/approve", json=payload)
    second = client.post("/ingest/publish/approve", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["publish_job_name"] == second.json()["publish_job_name"]
    assert first.json()["status"] == "publish_running"
    assert len(fake_transport.submitted) == 1
    assert sqlite_ledger.status(*identity).status == "publish_running"


def _auto_publish_candidate(identity: tuple[str, str, str], *, pg4: str = "pass", pg5: str = "pass") -> dict:
    return {
        "epoch": identity[0],
        "category": identity[1],
        "manifest_sha": identity[2],
        "run_id": "build-run",
        "activation_journal": "/market-output/.ubist_activation_build_run.json",
        "candidate_integrity": {"file_count": 65, "total_bytes": 100, "manifest_sha": "c" * 64},
        "build_table_integrity": [{"table": "mart_general_brand_metric", "row_count": 1}],
        "automatic_publish": {
            "hard_gates": {
                "PG-1": "pass",
                "PG-2": "pass",
                "PG-3": "pass",
                "PG-4": pg4,
                "PG-5": pg5,
            },
            "warnings": {"PG-6": "warning", "PG-7": "warning"},
        },
    }


def test_post_gate_candidate_auto_queues_publish_without_human_approval(
    sqlite_ledger,
    fake_transport,
) -> None:
    identity = _seed(sqlite_ledger, epoch="2026-06", category="ubist", manifest_sha="c" * 64)
    sqlite_ledger.mark_running(*identity, job_name="build", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate=_auto_publish_candidate(identity),
        prepared_at="2026-08-07T00:00:00+00:00",
        expires_at="2026-08-08T00:00:00+00:00",
    )
    client = TestClient(create_app(IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "publish-run",
        timestamp=lambda: "2026-08-07T01:00:00+00:00",
    )))

    response = client.post("/ingest/publish/automatic", json={
        "epoch": identity[0],
        "category": identity[1],
        "manifest_sha": identity[2],
        "run_id": "build-run",
    })

    assert response.status_code == 200
    assert response.json()["status"] == "publish_running"
    assert len(fake_transport.submitted) == 1
    assert sqlite_ledger.prepared_candidate(*identity).approved_by == "system:full-scan-auto-publish"


def test_csd_candidate_auto_queues_publish_with_source_integrity(
    sqlite_ledger,
    fake_transport,
) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="iqvia_csd_channel",
        manifest_sha="f" * 64,
    )
    sqlite_ledger.mark_running(*identity, job_name="build", run_id="build-run")
    candidate = {
        "epoch": identity[0],
        "category": identity[1],
        "manifest_sha": identity[2],
        "run_id": "build-run",
        "csd_activation_plan": {"run_id": "build-run"},
        "csd_candidate_evidence": {
            "raw": {"row_count": 1, "crc_sum": 2, "crc_xor": 2},
            "stage": {"row_count": 1, "crc_sum": 3, "crc_xor": 3},
        },
        "automatic_publish": {
            "hard_gates": {f"PG-{index}": "pass" for index in range(1, 6)},
            "warnings": {"PG-6": "warning", "PG-7": "warning"},
        },
    }
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate=candidate,
        prepared_at="2026-08-07T00:00:00+00:00",
        expires_at="2026-08-08T00:00:00+00:00",
    )
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: "publish-run",
                timestamp=lambda: "2026-08-07T01:00:00+00:00",
            )
        )
    )

    response = client.post(
        "/ingest/publish/automatic",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "publish_running"
    assert len(fake_transport.submitted) == 1


def test_keyword_candidate_auto_queues_publish_with_source_integrity(
    sqlite_ledger,
    fake_transport,
) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="iqvia_csd_keyword",
        manifest_sha="9" * 64,
    )
    sqlite_ledger.mark_running(*identity, job_name="build", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={
            "keyword_activation_plan": {"run_id": "build-run"},
            "keyword_candidate_evidence": {"raw_rows": 10, "stage_rows": 8},
            "automatic_publish": {
                "hard_gates": {f"PG-{index}": "pass" for index in range(1, 6)}
            },
        },
        prepared_at="2026-08-07T00:00:00+00:00",
        expires_at="2026-08-08T00:00:00+00:00",
    )
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: "publish-run",
                timestamp=lambda: "2026-08-07T01:00:00+00:00",
            )
        )
    )

    response = client.post(
        "/ingest/publish/automatic",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
        },
    )

    assert response.status_code == 200
    assert len(fake_transport.submitted) == 1


def test_startup_drain_recovers_automatic_publish_callback(
    sqlite_ledger,
    fake_transport,
) -> None:
    identity = _seed(sqlite_ledger, epoch="2026-06", category="ubist", manifest_sha="e" * 64)
    sqlite_ledger.mark_running(*identity, job_name="build", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate=_auto_publish_candidate(identity),
        prepared_at="2026-08-07T00:00:00+00:00",
        expires_at="2026-08-08T00:00:00+00:00",
    )
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "publish-run",
        timestamp=lambda: "2026-08-07T01:00:00+00:00",
    )

    result = service.drain_idle_queues()

    assert result["automatic_publishes"]["ubist"].startswith("jw-ingest-publish-")
    assert result["errors"] == {}
    assert sqlite_ledger.status(*identity).status == "publish_running"


@pytest.mark.parametrize(("pg4", "pg5"), [("fail", "pass"), ("pass", "fail")])
def test_pg4_or_pg5_failure_cannot_auto_publish(
    sqlite_ledger,
    fake_transport,
    pg4: str,
    pg5: str,
) -> None:
    identity = _seed(sqlite_ledger, epoch="2026-06", category="ubist", manifest_sha="d" * 64)
    sqlite_ledger.mark_running(*identity, job_name="build", run_id="build-run")
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate=_auto_publish_candidate(identity, pg4=pg4, pg5=pg5),
        prepared_at="2026-08-07T00:00:00+00:00",
        expires_at="2026-08-08T00:00:00+00:00",
    )
    client = TestClient(create_app(IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        timestamp=lambda: "2026-08-07T01:00:00+00:00",
    )))

    response = client.post("/ingest/publish/automatic", json={
        "epoch": identity[0],
        "category": identity[1],
        "manifest_sha": identity[2],
        "run_id": "build-run",
    })

    assert response.status_code == 409
    assert fake_transport.submitted == []
    assert sqlite_ledger.status(*identity).status == "awaiting_approval"


def test_publish_approval_rejects_identity_mismatch_and_expired_candidate(
    sqlite_ledger,
    fake_transport,
    monkeypatch,
) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-bbbbbbbb-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "activation_journal": "/market-output/.ubist_activation_build_run.json",
        },
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-04T00:30:00+00:00",
    )
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        timestamp=lambda: "2026-08-04T01:00:00+00:00",
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        service,
        "_cleanup_expired_publish_candidate",
        lambda candidate: cleaned.append(candidate.build_run_id),
    )
    client = TestClient(create_app(service))

    mismatch = client.post(
        "/ingest/publish/approve",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "wrong-run",
            "requested_by": "pl@example.com",
        },
    )
    expired = client.post(
        "/ingest/publish/approve",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "requested_by": "pl@example.com",
        },
    )

    assert mismatch.status_code == 409
    assert expired.status_code == 409
    assert "expired" in expired.json()["detail"]
    assert fake_transport.submitted == []
    assert sqlite_ledger.status(*identity).status == "failed"
    assert sqlite_ledger.running_in_category("ubist") == 0
    assert cleaned == ["build-run"]


def test_publish_submission_failure_restores_retryable_approval_state(sqlite_ledger) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="c" * 64,
    )
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-cccccccc-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )

    def fail_transport(_url, _body):
        raise RuntimeError("injected Kubernetes submission failure")

    def absent(_namespace, name):
        raise urllib.error.HTTPError(name, 404, "Not Found", {}, None)

    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fail_transport,
                inspect_transport=absent,
                now=lambda: "publish-run",
                timestamp=lambda: "2026-08-04T01:00:00+00:00",
            )
        )
    )
    response = client.post(
        "/ingest/publish/approve",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "requested_by": "pl@example.com",
        },
    )

    assert response.status_code == 503
    assert sqlite_ledger.status(*identity).status == "awaiting_approval"
    assert sqlite_ledger.prepared_candidate(*identity).publish_job_name is None


def test_publish_submission_timeout_keeps_running_when_job_exists(sqlite_ledger) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="d" * 64,
    )
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-dddddddd-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )

    def timeout_after_create(_url, _body):
        raise TimeoutError("injected response loss after Job creation")

    inspected: list[str] = []

    def running(_namespace, name):
        inspected.append(name)
        return {
            "metadata": {"name": name, "uid": "publish-job-uid"},
            "status": {"active": 1},
        }

    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=timeout_after_create,
                inspect_transport=running,
                now=lambda: "publish-run",
                timestamp=lambda: "2026-08-04T01:00:00+00:00",
            )
        )
    )

    response = client.post(
        "/ingest/publish/approve",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "requested_by": "pl@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "publish_running"
    assert response.json()["submission_reconciled"] is True
    candidate = sqlite_ledger.prepared_candidate(*identity)
    assert sqlite_ledger.status(*identity).status == "publish_running"
    assert inspected == [candidate.publish_job_name]


def test_publish_submission_inspection_failure_stays_fail_closed(sqlite_ledger) -> None:
    identity = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="e" * 64,
    )
    sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-eeeeeeee-build",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *identity,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-05T00:00:00+00:00",
    )

    def unavailable(*_args):
        raise TimeoutError("injected Kubernetes API outage")

    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=unavailable,
                inspect_transport=unavailable,
                now=lambda: "publish-run",
                timestamp=lambda: "2026-08-04T01:00:00+00:00",
            )
        )
    )

    response = client.post(
        "/ingest/publish/approve",
        json={
            "epoch": identity[0],
            "category": identity[1],
            "manifest_sha": identity[2],
            "run_id": "build-run",
            "requested_by": "pl@example.com",
        },
    )

    assert response.status_code == 500
    assert "requires reconciliation" in response.json()["detail"]
    assert sqlite_ledger.status(*identity).status == "publish_running"
    assert sqlite_ledger.prepared_candidate(*identity).publish_job_name is not None


def test_promote_expires_stale_candidate_and_launches_next_queued_entry(
    sqlite_ledger,
    fake_transport,
) -> None:
    expired = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *expired,
        job_name="build-job",
        run_id="build-run",
    )
    sqlite_ledger.mark_awaiting_approval(
        *expired,
        run_id="build-run",
        candidate={"run_id": "build-run"},
        prepared_at="2026-08-04T00:00:00+00:00",
        expires_at="2026-08-04T01:00:00+00:00",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260804020000000000",
        timestamp=lambda: "2026-08-04T02:00:00+00:00",
    )

    job_name = service.promote("ubist")

    assert job_name is not None
    assert sqlite_ledger.status(*expired).status == "failed"
    assert "expired" in sqlite_ledger.status(*expired).reason
    assert sqlite_ledger.status(*queued).status == "running"
    assert len(fake_transport.submitted) == 1


def test_unknown_status_keeps_existing_404_contract(sqlite_ledger) -> None:
    response = TestClient(create_app(IngestService(sqlite_ledger, None))).get(
        "/ingest/status",
        params={
            "epoch": "2026-06",
            "category": "ubist",
            "manifest_sha": "f" * 64,
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown submission identity"}


def test_concurrent_promotions_reserve_only_one_running_entry(
    sqlite_ledger,
    fake_transport,
) -> None:
    for index in range(3):
        _seed(
            sqlite_ledger,
            epoch=f"2026-0{index + 5}",
            category="ubist",
            manifest_sha=str(index + 1) * 64,
        )
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        inspect_transport=lambda _namespace, name: {
            "metadata": {"name": name},
            "status": {"active": 1},
        },
        now=lambda: "20260729010101000000",
    )
    barrier = threading.Barrier(8)
    results: list[str | None] = []
    errors: list[BaseException] = []

    def promote() -> None:
        barrier.wait()
        try:
            results.append(service.promote("ubist"))
        except BaseException as exc:  # test must expose every concurrent failure
            errors.append(exc)

    threads = [threading.Thread(target=promote) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert sum(result is not None for result in results) == 1
    assert sqlite_ledger.running_in_category("ubist") == 1
    assert len(fake_transport.submitted) == 1


def test_terminal_callback_promotes_next_only_after_slot_release(
    sqlite_ledger,
    fake_transport,
) -> None:
    running = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *running,
        job_name="jw-ingest-ubist-aaaaaaaa-run",
        run_id="run",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: "20260729020202000000",
            )
        )
    )
    signal = {
        "event": "complete",
        "mode": "staging",
        "category": running[1],
        "epoch": running[0],
        "manifest_sha": running[2],
        "rows_before": 0,
        "rows_after": 1,
        "rows_loaded": 1,
        "period": {"from": "2026-05", "to": "2026-05"},
        "started_at": "2026-07-29T00:00:00+00:00",
        "finished_at": "2026-07-29T00:01:00+00:00",
        "failure_reason": None,
        "log_ref": "/ingest/status",
    }

    blocked = client.post("/ingest/terminal", json=signal)
    assert blocked.status_code == 409
    assert sqlite_ledger.status(*queued).status == "queued"

    sqlite_ledger.mark_complete(*running, row_counts={"input.xlsx": 1})
    drained = client.post("/ingest/terminal", json=signal)

    assert drained.status_code == 200
    assert drained.json()["accepted"] is True
    assert drained.json()["promoted_job_name"] == sqlite_ledger.status(*queued).job_name
    assert sqlite_ledger.running_in_category("ubist") == 1

    queue_payload = client.get("/ingest/queue").json()
    assert [
        (
            item["epoch"],
            item["status"],
            item["blocked_by_category"],
            item["requires_reconcile"],
        )
        for item in queue_payload["items"]
    ] == [("2026-06", "running", False, False)]
    status_payload = client.get(
        "/ingest/status",
        params={
            "epoch": queued[0],
            "category": queued[1],
            "manifest_sha": queued[2],
        },
    ).json()
    assert status_payload["status"] == "running"
    assert status_payload["blocked_by_category"] is False
    assert status_payload["requires_reconcile"] is False
    assert [item["stage"] for item in status_payload["expected_stages"]] == [
        "job_submit",
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "dashboard",
        "signal",
    ]


def test_failed_terminal_callback_promotes_next_after_failed_slot_release(
    sqlite_ledger,
    fake_transport,
) -> None:
    failed = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *failed,
        job_name="jw-ingest-ubist-aaaaaaaa-run",
        run_id="run",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    sqlite_ledger.mark_failed(*failed, reason="injected failure")
    client = TestClient(
        create_app(
            IngestService(
                sqlite_ledger,
                None,
                transport=fake_transport,
                now=lambda: "20260729030303000000",
            )
        )
    )

    response = client.post(
        "/ingest/terminal",
        json={
            "event": "failed",
            "mode": "staging",
            "category": failed[1],
            "epoch": failed[0],
            "manifest_sha": failed[2],
            "rows_before": 0,
            "rows_after": 0,
            "rows_loaded": 0,
            "period": {"from": None, "to": None},
            "started_at": "2026-07-29T00:00:00+00:00",
            "finished_at": "2026-07-29T00:01:00+00:00",
            "failure_reason": "injected failure",
            "log_ref": "/ingest/status",
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert sqlite_ledger.status(*queued).status == "running"


def test_terminal_callback_rejects_unknown_identity_without_promotion(
    sqlite_ledger,
    fake_transport,
) -> None:
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )
    client = TestClient(
        create_app(IngestService(sqlite_ledger, None, transport=fake_transport))
    )

    response = client.post(
        "/ingest/terminal",
        json={
            "event": "failed",
            "mode": "staging",
            "category": "ubist",
            "epoch": "2026-05",
            "manifest_sha": "a" * 64,
            "rows_before": 0,
            "rows_after": 0,
            "rows_loaded": 0,
            "period": {"from": None, "to": None},
            "started_at": "2026-07-29T00:00:00+00:00",
            "finished_at": "2026-07-29T00:01:00+00:00",
            "failure_reason": "injected failure",
            "log_ref": "/ingest/status",
        },
    )

    assert response.status_code == 404
    assert sqlite_ledger.status(*queued).status == "queued"
    assert fake_transport.submitted == []


def test_runner_publishes_terminal_signal_to_internal_drain_callback(
    monkeypatch,
    sqlite_ledger,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.config.completion_webhook",
        lambda: ("", 3),
    )
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.config.queue_drain_webhook",
        lambda: ("http://jw-ingest-hook/ingest/terminal", 3),
    )
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.completion_signal.publish",
        lambda signal, *, endpoint, attempts: (
            calls.append((signal.event, endpoint))
            or PublishResult("published", 1, None)
        ),
    )
    tracker_reasons: list[str] = []
    tracker = type(
        "Tracker",
        (),
        {
            "complete": lambda _self, _stage, *, reason: tracker_reasons.append(
                reason
            )
        },
    )()

    job_runner._emit_completion_signal(
        ledger=sqlite_ledger,
        tracker=tracker,
        identity=("2026-06", "ubist", "a" * 64),
        run_id="run",
        event="failed",
        mode="staging",
        rows_before=0,
        rows_after=0,
        rows_loaded=0,
        periods=set(),
        started_at="2026-07-29T00:00:00+00:00",
        failure_reason="injected failure",
    )

    assert calls == [
        ("failed", ""),
        ("failed", "http://jw-ingest-hook/ingest/terminal"),
    ]
    assert "queue_drain=published" in tracker_reasons[0]


def test_rendered_job_inherits_internal_drain_callback(monkeypatch) -> None:
    monkeypatch.setenv(
        "INGEST_QUEUE_DRAIN_WEBHOOK_URL",
        "http://jw-ingest-hook/ingest/terminal",
    )
    monkeypatch.setenv("INGEST_QUEUE_DRAIN_WEBHOOK_ATTEMPTS", "3")

    body = render_job(
        category="ubist",
        manifest_sha="a" * 64,
        manifest_path="/input/manifest.json",
        namespace="llmops",
    )

    env = {
        item["name"]: item
        for item in body["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INGEST_QUEUE_DRAIN_WEBHOOK_URL"]["value"] == (
        "http://jw-ingest-hook/ingest/terminal"
    )
    assert env["INGEST_QUEUE_DRAIN_WEBHOOK_ATTEMPTS"]["value"] == "3"


def test_startup_drain_recovers_queue_after_missed_terminal_callback(
    sqlite_ledger,
    fake_transport,
) -> None:
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="a" * 64,
    )
    app = create_app(
        IngestService(
            sqlite_ledger,
            None,
            transport=fake_transport,
            now=lambda: "20260729040404000000",
        )
    )

    with TestClient(app):
        pass

    assert sqlite_ledger.status(*queued).status == "running"
    assert app.state.startup_queue_drain == {
        "launched": {
            "global": sqlite_ledger.status(*queued).job_name,
        },
        "errors": {},
    }


def test_concurrent_startup_drains_keep_one_running_per_category(
    monkeypatch,
    sqlite_ledger,
    fake_transport,
) -> None:
    for index in range(3):
        _seed(
            sqlite_ledger,
            epoch=f"2026-0{index + 5}",
            category="ubist",
            manifest_sha=str(index + 1) * 64,
        )
    first = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260729050505000000",
    )
    second = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260729050505000001",
    )
    original_claim = sqlite_ledger.claim_queued
    claim_barrier = threading.Barrier(2)

    def racing_claim(*args, **kwargs):
        claim_barrier.wait()
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(sqlite_ledger, "claim_queued", racing_claim)
    results: list[dict] = []
    errors: list[BaseException] = []

    def drain(service: IngestService) -> None:
        try:
            results.append(service.drain_idle_queues())
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=drain, args=(first,)),
        threading.Thread(target=drain, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert sqlite_ledger.running_in_category("ubist") == 1
    assert len(fake_transport.submitted) == 1
    assert len(sqlite_ledger.active_entries("ubist")) == 3


def test_startup_drain_reports_one_category_failure_and_continues(
    sqlite_ledger,
    fake_transport,
) -> None:
    _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="a" * 64,
    )
    iqvia = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="iqvia_nsa",
        manifest_sha="b" * 64,
    )
    submissions = 0

    def fail_first_submission(url: str, body: dict) -> dict:
        nonlocal submissions
        submissions += 1
        if submissions == 1:
            raise RuntimeError("injected startup drain failure")
        return fake_transport(url, body)

    service = IngestService(
        sqlite_ledger,
        None,
        transport=fail_first_submission,
        now=lambda: "20260729060606000000",
    )
    app = create_app(service)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}

    assert app.state.startup_queue_drain["launched"] == {
        "global": sqlite_ledger.status(*iqvia).job_name,
    }
    assert app.state.startup_queue_drain["errors"] == {
        "global": "RuntimeError: injected startup drain failure",
    }
