from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from pipeline.scripts.api.audit_logging import (
    AsyncAuditWriter,
    AuditEvent,
    install_audit_logging_middleware,
)
from pipeline.scripts.api.audit_retention import delete_expired_rows


class CollectingSubmitter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def submit(self, event: AuditEvent) -> bool:
        self.events.append(event)
        return True


def _app(submitter: CollectingSubmitter) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def actor_context(request: Request, call_next):
        request.state.actor_type = request.headers.get("X-Test-Actor-Type", "unknown")
        request.state.actor_uid = request.headers.get("X-Test-Actor-Uid")
        request.state.jti = request.headers.get("X-Test-Jti")
        return await call_next(request)

    @app.post("/api/items/{item_id}")
    async def items(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/failure")
    async def failure() -> None:
        raise RuntimeError("expected test failure")

    install_audit_logging_middleware(app, submitter)
    return app


def test_records_actor_route_query_body_status_and_jti() -> None:
    submitter = CollectingSubmitter()
    client = TestClient(_app(submitter))

    response = client.post(
        "/api/items/7?market_id=ml_001&market_id=ml_002",
        json={"brand": "sample", "filters": {"source": "ubist"}},
        headers={
            "X-Test-Actor-Type": "user",
            "X-Test-Actor-Uid": "genos-user:123",
            "X-Test-Jti": "jti-123",
        },
    )

    assert response.status_code == 200
    assert len(submitter.events) == 1
    event = submitter.events[0]
    assert event.actor_type == "user"
    assert event.actor_uid == "genos-user:123"
    assert event.jti == "jti-123"
    assert event.endpoint == "POST /api/items/{item_id}"
    assert event.http_status == 200
    assert event.request_params == {
        "path": {"item_id": "7"},
        "query": {"market_id": ["ml_001", "ml_002"]},
        "body": {"brand": "sample", "filters": {"source": "ubist"}},
    }


def test_missing_assertion_is_recorded_as_unknown_without_blocking() -> None:
    submitter = CollectingSubmitter()

    response = TestClient(_app(submitter)).post("/api/items/8", json={"brand": "sample"})

    assert response.status_code == 200
    assert submitter.events[0].actor_type == "unknown"
    assert submitter.events[0].actor_uid is None


def test_warm_request_is_recorded_as_system() -> None:
    submitter = CollectingSubmitter()

    response = TestClient(_app(submitter)).post(
        "/api/items/9",
        json={"brand": "sample"},
        headers={"X-Market-System-Actor": "cache-warm"},
    )

    assert response.status_code == 200
    assert submitter.events[0].actor_type == "system"
    assert submitter.events[0].actor_uid is None


def test_health_is_excluded() -> None:
    submitter = CollectingSubmitter()

    response = TestClient(_app(submitter)).get("/api/health")

    assert response.status_code == 200
    assert submitter.events == []


def test_handler_failure_is_recorded_and_the_original_error_contract_is_preserved() -> None:
    submitter = CollectingSubmitter()

    response = TestClient(_app(submitter), raise_server_exceptions=False).get("/api/failure")

    assert response.status_code == 500
    assert len(submitter.events) == 1
    assert submitter.events[0].endpoint == "GET /api/failure"
    assert submitter.events[0].http_status == 500


def test_writer_failure_is_logged_without_affecting_api_response(caplog) -> None:
    def failing_sink(_events: list[AuditEvent]) -> None:
        raise RuntimeError("audit sink unavailable")

    writer = AsyncAuditWriter(failing_sink, queue_capacity=4, batch_size=1)
    writer.start()
    try:
        with caplog.at_level(logging.ERROR):
            response = TestClient(_app(writer)).post("/api/items/10", json={"brand": "sample"})
            deadline = time.monotonic() + 2
            while "audit log write failed" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        writer.stop()

    assert response.status_code == 200
    assert "audit log write failed" in caplog.text


class FakeCursor:
    def __init__(self, row_counts: list[int]) -> None:
        self.row_counts = row_counts
        self.rowcount = 0
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))
        self.rowcount = self.row_counts.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeConnection:
    def __init__(self, row_counts: list[int]) -> None:
        self.cursor_value = FakeCursor(row_counts)
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1


def test_ttl_delete_is_cutoff_based_and_chunked() -> None:
    connection = FakeConnection([2, 1])
    now = datetime(2026, 7, 30, tzinfo=UTC)

    deleted = delete_expired_rows(
        connection,
        retention_days=90,
        batch_size=2,
        now=now,
    )

    assert deleted == 3
    assert connection.commits == 2
    assert len(connection.cursor_value.calls) == 2
    sql, params = connection.cursor_value.calls[0]
    assert "called_at < %s" in sql
    assert "ORDER BY called_at ASC, id ASC" in sql
    assert "LIMIT %s" in sql
    assert params == ((now - timedelta(days=90)).replace(tzinfo=None), 2)
