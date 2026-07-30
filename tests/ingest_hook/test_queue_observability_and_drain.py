"""Queue observability and event-driven category drain contracts."""
from __future__ import annotations

import threading

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
        "current_stage",
        "stages",
        "signals",
        "log_ref",
        "blocked_by_category",
        "requires_reconcile",
        "category_blocker",
        "expected_stages",
    }


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
    assert [item["stage"] for item in status_payload["expected_stages"]] == list(
        job_runner._StageTracker.STAGES
    )
    assert len(status_payload["expected_stages"]) == 9


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
            "ubist": sqlite_ledger.status(*queued).job_name,
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
    monkeypatch,
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
    service = IngestService(
        sqlite_ledger,
        None,
        transport=fake_transport,
        now=lambda: "20260729060606000000",
    )
    original_promote = service.promote

    def failing_promote(category: str) -> str | None:
        if category == "ubist":
            raise RuntimeError("injected startup drain failure")
        return original_promote(category)

    monkeypatch.setattr(service, "promote", failing_promote)
    app = create_app(service)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}

    assert app.state.startup_queue_drain["launched"] == {
        "iqvia_nsa": sqlite_ledger.status(*iqvia).job_name,
    }
    assert app.state.startup_queue_drain["errors"] == {
        "ubist": "RuntimeError: injected startup drain failure",
    }
