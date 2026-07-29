"""Read-only ingest queue route contracts."""
from __future__ import annotations

from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook.app import IngestService, create_app


QUEUE_ITEM_KEYS = {
    "category",
    "epoch",
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


def _seed(sqlite_ledger, *, epoch: str, category: str, manifest_sha: str) -> tuple[str, str, str]:
    identity = (epoch, category, manifest_sha)
    sqlite_ledger.receive(
        *identity,
        manifest_path=f"/input/{manifest_sha[:8]}.json",
        uploaded_by="queue-route-test",
    )
    return identity


def _client(sqlite_ledger) -> TestClient:
    return TestClient(create_app(IngestService(sqlite_ledger, input_root=None)))


def test_queue_lists_active_entries_with_portal_contract(sqlite_ledger) -> None:
    running = _seed(
        sqlite_ledger,
        epoch="2026-05",
        category="ubist",
        manifest_sha="a" * 64,
    )
    sqlite_ledger.mark_running(
        *running,
        job_name="jw-ingest-ubist-aaaaaaaa-run",
        run_id="run-1",
    )
    queued = _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="b" * 64,
    )

    response = _client(sqlite_ledger).get("/ingest/queue")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [set(item) for item in items] == [QUEUE_ITEM_KEYS, QUEUE_ITEM_KEYS]
    assert [
        (item["epoch"], item["category"], item["manifest_sha"], item["status"])
        for item in items
    ] == [(*running, "running"), (*queued, "queued")]
    assert items[0]["blocked_by_category"] is False
    assert items[0]["requires_reconcile"] is False
    assert items[1]["blocked_by_category"] is True
    assert items[1]["requires_reconcile"] is False


def test_queue_returns_empty_items_for_empty_ledger(sqlite_ledger) -> None:
    response = _client(sqlite_ledger).get("/ingest/queue")

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

    response = _client(sqlite_ledger).get("/ingest/queue")

    assert response.status_code == 200
    assert [
        (item["epoch"], item["category"], item["manifest_sha"])
        for item in response.json()["items"]
    ] == [allowed]


def test_queue_returns_empty_items_for_unknown_category_filter(sqlite_ledger) -> None:
    _seed(
        sqlite_ledger,
        epoch="2026-06",
        category="ubist",
        manifest_sha="e" * 64,
    )

    response = _client(sqlite_ledger).get(
        "/ingest/queue",
        params={"category": "not-configured"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}
