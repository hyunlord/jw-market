from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from threading import Thread
from typing import Any, Final, Protocol

import pymysql
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from pipeline.scripts.api.actor_assertion import actor_from_request
from pipeline.scripts.api.config import APIConfig

LOGGER = logging.getLogger(__name__)
SYSTEM_ACTOR_HEADER: Final = "X-Market-System-Actor"
SYSTEM_ACTOR_CACHE_WARM: Final = "cache-warm"
EXCLUDED_PATHS: Final = frozenset({"/", "/api/health", "/api/capabilities"})
SENSITIVE_KEYS: Final = frozenset(
    {
        "authorization",
        "authorization-access-token",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "x-actor-assertion",
    }
)


@dataclass(frozen=True)
class AuditEvent:
    actor_uid: str | None
    actor_type: str
    called_at: datetime
    endpoint: str
    request_params: dict[str, Any]
    http_status: int
    jti: str | None


class AuditSubmitter(Protocol):
    def submit(self, event: AuditEvent) -> bool: ...


class DisabledAuditSubmitter:
    def submit(self, event: AuditEvent) -> bool:
        return False


class MariaDBAuditSink:
    _INSERT_SQL: Final = """
        INSERT INTO audit_api_call_log
            (actor_uid, actor_type, called_at, endpoint, request_params, http_status, jti)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
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
            raise ValueError(f"audit logging enabled but required settings are missing: {', '.join(missing)}")
        self._connect_args = {
            "host": config.audit_db_host,
            "port": config.audit_db_port,
            "user": config.audit_db_user,
            "password": config.audit_db_password,
            "database": config.audit_db_name,
            "charset": "utf8mb4",
            "autocommit": False,
        }

    def __call__(self, events: list[AuditEvent]) -> None:
        rows = [
            (
                event.actor_uid,
                event.actor_type,
                event.called_at,
                event.endpoint,
                json.dumps(event.request_params, ensure_ascii=False, separators=(",", ":")),
                event.http_status,
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


class AsyncAuditWriter:
    _STOP: Final = object()

    def __init__(
        self,
        sink: Callable[[list[AuditEvent]], None],
        *,
        queue_capacity: int,
        batch_size: int,
    ) -> None:
        if queue_capacity < 1 or batch_size < 1:
            raise ValueError("audit queue capacity and batch size must be positive")
        self._sink = sink
        self._batch_size = batch_size
        self._queue: Queue[AuditEvent | object] = Queue(maxsize=queue_capacity)
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="market-audit-writer", daemon=True)
        self._thread.start()

    def submit(self, event: AuditEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            LOGGER.error("audit log queue full; event dropped endpoint=%s", event.endpoint)
            return False

    def stop(self, timeout_seconds: float = 10) -> None:
        thread = self._thread
        if thread is None:
            return
        self._queue.put(self._STOP)
        thread.join(timeout_seconds)
        if thread.is_alive():
            LOGGER.error("audit log writer did not stop within timeout")
        self._thread = None

    def _run(self) -> None:
        stopping = False
        while not stopping:
            item = self._queue.get()
            if item is self._STOP:
                stopping = True
                batch: list[AuditEvent] = []
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
                    LOGGER.exception("audit log write failed batch_size=%d", len(batch))


class AuditLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, submitter: AuditSubmitter) -> None:
        super().__init__(app)
        self._submitter = submitter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        body = await request.body()
        try:
            response = await call_next(request)
        except Exception:
            self._submit(request, body, 500)
            raise
        self._submit(request, body, response.status_code)
        return response

    def _submit(self, request: Request, body: bytes, status_code: int) -> None:
        actor = actor_from_request(request)
        actor_type, actor_uid, jti = actor.actor_type, actor.actor_uid, actor.jti
        if actor_type == "unknown" and request.headers.get(SYSTEM_ACTOR_HEADER) == SYSTEM_ACTOR_CACHE_WARM:
            actor_type, actor_uid, jti = "system", None, None

        self._submitter.submit(
            AuditEvent(
                actor_uid=actor_uid,
                actor_type=actor_type,
                called_at=datetime.now(UTC).replace(tzinfo=None),
                endpoint=f"{request.method} {_route_path(request)}",
                request_params=_request_params(request, body),
                http_status=status_code,
                jti=jti,
            )
        )


def install_audit_logging_middleware(app: FastAPI, submitter: AuditSubmitter) -> None:
    app.add_middleware(AuditLogMiddleware, submitter=submitter)


def create_audit_writer(config: APIConfig) -> AsyncAuditWriter | DisabledAuditSubmitter:
    if not config.audit_log_enabled:
        return DisabledAuditSubmitter()
    return AsyncAuditWriter(
        MariaDBAuditSink(config),
        queue_capacity=config.audit_log_queue_capacity,
        batch_size=config.audit_log_batch_size,
    )


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else request.url.path


def _request_params(request: Request, body: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if request.path_params:
        result["path"] = _redact(dict(request.path_params))
    query: dict[str, str | list[str]] = {}
    for key in request.query_params:
        values = request.query_params.getlist(key)
        query[key] = values[0] if len(values) == 1 else values
    if query:
        result["query"] = _redact(query)
    if body and "application/json" in request.headers.get("content-type", ""):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            result["body"] = {"_invalid_json": True}
        else:
            result["body"] = _redact(parsed)
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
