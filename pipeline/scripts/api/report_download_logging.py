from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from threading import Thread
from typing import Final, Literal, Protocol

import pymysql
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from pipeline.scripts.api.actor_assertion import actor_from_request
from pipeline.scripts.api.config import APIConfig

LOGGER = logging.getLogger(__name__)


class ReportDownloadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    report_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    completion_stage: Literal["upstream_response", "browser_payload_ready"]
    success: bool
    trace_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True, slots=True)
class ReportDownloadEvent:
    actor_uid: str | None
    actor_type: str
    completed_at: datetime
    report_type: str
    report_id: str
    completion_stage: str
    success: bool
    trace_id: str | None
    jti: str | None


class ReportDownloadSubmitter(Protocol):
    def submit(self, event: ReportDownloadEvent) -> bool: ...


class DisabledReportDownloadSubmitter:
    def submit(self, event: ReportDownloadEvent) -> bool:
        return False


class MariaDBReportDownloadSink:
    _INSERT_SQL: Final = """
        INSERT INTO report_download_event
            (actor_uid, actor_type, completed_at, report_type, report_id,
             completion_stage, success, trace_id, jti)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    def __init__(self, config: APIConfig) -> None:
        required = {
            "AUDIT_DB_HOST": config.audit_db_host,
            "AUDIT_DB_USER": config.audit_db_user,
            "AUDIT_DB_PASSWORD": config.audit_db_password,
            "AUDIT_DB_NAME": config.audit_db_name,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "report download logging enabled but required settings are missing: "
                + ", ".join(missing)
            )
        self._connect_args = {
            "host": config.audit_db_host,
            "port": config.audit_db_port,
            "user": config.audit_db_user,
            "password": config.audit_db_password,
            "database": config.audit_db_name,
            "charset": "utf8mb4",
            "autocommit": False,
        }

    def __call__(self, events: list[ReportDownloadEvent]) -> None:
        rows = [
            (
                event.actor_uid,
                event.actor_type,
                event.completed_at,
                event.report_type,
                event.report_id,
                event.completion_stage,
                event.success,
                event.trace_id,
                event.jti,
            )
            for event in events
        ]
        connection = pymysql.connect(**self._connect_args)
        try:
            with connection.cursor() as cursor:
                cursor.executemany(self._INSERT_SQL, rows)
            connection.commit()
        finally:
            connection.close()


class AsyncReportDownloadWriter:
    _STOP: Final = object()

    def __init__(
        self,
        sink: Callable[[list[ReportDownloadEvent]], None],
        *,
        queue_capacity: int,
        batch_size: int,
    ) -> None:
        if queue_capacity < 1 or batch_size < 1:
            raise ValueError("report telemetry queue capacity and batch size must be positive")
        self._sink = sink
        self._batch_size = batch_size
        self._queue: Queue[ReportDownloadEvent | object] = Queue(maxsize=queue_capacity)
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = Thread(target=self._run, name="report-download-writer", daemon=True)
            self._thread.start()

    def submit(self, event: ReportDownloadEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            LOGGER.error("report download telemetry queue full; event dropped")
            return False

    def stop(self, timeout_seconds: float = 10) -> None:
        thread = self._thread
        if thread is None:
            return
        self._queue.put(self._STOP)
        thread.join(timeout_seconds)
        if thread.is_alive():
            LOGGER.error("report download telemetry writer did not stop within timeout")
        self._thread = None

    def _run(self) -> None:
        stopping = False
        while not stopping:
            item = self._queue.get()
            if item is self._STOP:
                stopping = True
                batch: list[ReportDownloadEvent] = []
            else:
                batch = [item]
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
                if item is self._STOP:
                    stopping = True
                    break
                batch.append(item)
            if batch:
                try:
                    self._sink(batch)
                except Exception:
                    LOGGER.exception("report download telemetry write failed batch_size=%d", len(batch))


def create_report_download_writer(
    config: APIConfig,
) -> AsyncReportDownloadWriter | DisabledReportDownloadSubmitter:
    if not config.audit_log_enabled:
        return DisabledReportDownloadSubmitter()
    return AsyncReportDownloadWriter(
        MariaDBReportDownloadSink(config),
        queue_capacity=config.audit_log_queue_capacity,
        batch_size=config.audit_log_batch_size,
    )


def create_report_download_router(submitter: ReportDownloadSubmitter) -> APIRouter:
    router = APIRouter(tags=["telemetry"])

    @router.post(
        "/api/telemetry/report-downloads",
        status_code=status.HTTP_202_ACCEPTED,
        summary="Record a report completion boundary",
        include_in_schema=False,
    )
    def report_download(payload: ReportDownloadPayload, request: Request) -> dict[str, bool]:
        actor = actor_from_request(request)
        accepted = submitter.submit(
            ReportDownloadEvent(
                actor_uid=actor.actor_uid,
                actor_type=actor.actor_type,
                completed_at=datetime.now(UTC).replace(tzinfo=None),
                report_type=payload.report_type,
                report_id=payload.report_id,
                completion_stage=payload.completion_stage,
                success=payload.success,
                trace_id=payload.trace_id,
                jti=actor.jti,
            )
        )
        if not accepted:
            LOGGER.error("report download telemetry queue rejected report_type=%s", payload.report_type)
        return {"accepted": accepted}

    return router
