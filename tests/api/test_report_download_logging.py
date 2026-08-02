from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from pipeline.scripts.api.report_download_logging import (
    AsyncReportDownloadWriter,
    ReportDownloadEvent,
    create_report_download_router,
)


class CollectingSubmitter:
    def __init__(self) -> None:
        self.events: list[ReportDownloadEvent] = []

    def submit(self, event: ReportDownloadEvent) -> bool:
        self.events.append(event)
        return True


def _app(submitter) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def actor_context(request: Request, call_next):
        request.state.actor_type = request.headers.get("X-Test-Actor-Type", "unknown")
        request.state.actor_uid = request.headers.get("X-Test-Actor-Uid")
        request.state.jti = request.headers.get("X-Test-Jti")
        return await call_next(request)

    app.include_router(create_report_download_router(submitter))
    return app


def test_records_only_report_metadata_at_explicit_completion_boundary() -> None:
    submitter = CollectingSubmitter()
    response = TestClient(_app(submitter)).post(
        "/api/telemetry/report-downloads",
        json={
            "report_type": "rnd_chat_pdf",
            "report_id": "report-123",
            "completion_stage": "browser_payload_ready",
            "success": True,
            "trace_id": "trace-123",
        },
        headers={"X-Test-Actor-Type": "user", "X-Test-Actor-Uid": "genos-user:34"},
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert len(submitter.events) == 1
    event = submitter.events[0]
    assert event.actor_uid == "genos-user:34"
    assert event.report_type == "rnd_chat_pdf"
    assert event.report_id == "report-123"
    assert event.completion_stage == "browser_payload_ready"
    assert event.success is True
    assert not hasattr(event, "question_text")
    assert not hasattr(event, "answer_text")


def test_queue_rejection_is_fail_open_and_observable(caplog) -> None:
    class RejectingSubmitter:
        def submit(self, _event: ReportDownloadEvent) -> bool:
            return False

    with caplog.at_level(logging.ERROR):
        response = TestClient(_app(RejectingSubmitter())).post(
            "/api/telemetry/report-downloads",
            json={
                "report_type": "market_analysis",
                "report_id": "report-456",
                "completion_stage": "upstream_response",
                "success": True,
            },
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": False}
    assert "report download telemetry queue rejected" in caplog.text


def test_sink_failure_does_not_change_endpoint_contract(caplog) -> None:
    def failing_sink(_events: list[ReportDownloadEvent]) -> None:
        raise RuntimeError("telemetry sink unavailable")

    writer = AsyncReportDownloadWriter(failing_sink, queue_capacity=4, batch_size=1)
    writer.start()
    try:
        with caplog.at_level(logging.ERROR):
            response = TestClient(_app(writer)).post(
                "/api/telemetry/report-downloads",
                json={
                    "report_type": "rnd_chat_pdf",
                    "report_id": "report-789",
                    "completion_stage": "browser_payload_ready",
                    "success": True,
                },
            )
            deadline = time.monotonic() + 2
            while "report download telemetry write failed" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        writer.stop()

    assert response.status_code == 202
    assert "report download telemetry write failed" in caplog.text


def test_rejects_content_like_payload_fields() -> None:
    response = TestClient(_app(CollectingSubmitter())).post(
        "/api/telemetry/report-downloads",
        json={
            "report_type": "rnd_chat_pdf",
            "report_id": "report-123",
            "completion_stage": "browser_payload_ready",
            "success": True,
            "question_text": "must not be accepted",
        },
    )

    assert response.status_code == 422
