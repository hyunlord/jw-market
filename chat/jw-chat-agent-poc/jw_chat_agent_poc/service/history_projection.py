from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Protocol
from uuid import uuid4

import pymysql


PROJECTION_ORIGIN = "jw-chat-agent-direct"
PROJECTION_VERSION = 1
LOGGER = logging.getLogger(__name__)
SAFE_HTTP_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "host",
        "user-agent",
        "x-forwarded-proto",
        "x-request-id",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectionRequestContext:
    portal_user_id: int | None
    http_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class CompletedTurn:
    source_log_id: int
    session_id: str
    turn_id: str
    turn_index: int
    question: str
    answer: str
    charts: tuple[dict[str, Any], ...]
    sources: tuple[str, ...]
    trace: dict[str, Any]
    timing: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectionJob:
    outbox_id: int
    turn: CompletedTurn
    projection_version: int
    trace_id: str
    span_id: str
    portal_user_id: int | None
    request_headers: dict[str, str]
    attempts: int


@dataclass(frozen=True, slots=True)
class ActiveChatService:
    service_id: int
    revision_id: int
    publication_id: int
    endpoint: str


class SessionProjectionWriter(Protocol):
    def active_service(self) -> ActiveChatService: ...

    def upsert_hidden(self, job: ProjectionJob, active: ActiveChatService) -> None: ...

    def mark_displayed(self, job: ProjectionJob) -> None: ...


class MongoProjectionWriter(Protocol):
    def upsert_and_verify(self, job: ProjectionJob, documents: tuple[dict, dict, dict]) -> bool: ...


def trusted_portal_user_id(
    raw_user_id: str | None,
    *,
    public_request: bool,
    api_key_authenticated: bool,
) -> int | None:
    if not public_request or not api_key_authenticated or raw_user_id is None:
        return None
    try:
        user_id = int(raw_user_id.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("X-Portal-User-Id must be a positive integer") from exc
    if user_id <= 0:
        raise ValueError("X-Portal-User-Id must be a positive integer")
    return user_id


def sanitize_http_headers(headers: Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for name, value in headers.items():
        normalized = name.lower().strip()
        if normalized in SAFE_HTTP_HEADERS:
            sanitized[normalized] = str(value)
    return sanitized


def build_projection_documents(
    job: ProjectionJob,
    active: ActiveChatService,
    *,
    pod: str,
    ip: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    turn = job.turn
    elapsed_ms = _number(turn.timing.get("total_elapsed_ms")) or 0.0
    common_markers = {
        "origin": PROJECTION_ORIGIN,
        "synthetic_history_projection": True,
        "history_projection_version": job.projection_version,
    }
    service_trace = {
        "service": "chat-api",
        "name": "middleware",
        "pod": pod,
        "ip": ip,
        "span_id": job.span_id,
        "trace_id": job.trace_id,
        "session_id": turn.session_id,
        "genos_resource_id": active.service_id,
        "genos_resource_revision_id": active.revision_id,
        "genos_resource_deploy_id": active.publication_id,
        "http_method": "POST",
        "http_path": f"/chat/v2/query/{active.endpoint}",
        "http_query": "",
        "http_headers": dict(job.request_headers),
        "created_at": turn.created_at,
        "success": 1,
        "duration": elapsed_ms / 1000.0,
        "billing_eligible": False,
        **common_markers,
    }
    request_doc = {
        "trace_id": job.trace_id,
        "span_id": job.span_id,
        "data": {
            "question": turn.question,
            "socketIOClientId": "",
            "uploads": [],
            "chatId": turn.session_id,
        },
        **common_markers,
    }
    rendered = {
        "agentFlowExecutedData": _agent_flow(turn),
        "chatId": turn.session_id,
        "chatMessageId": turn.turn_id,
        "question": turn.question,
        "sessionId": turn.session_id,
        "text": turn.answer,
        "genos_persist": {
            "chat_agent_answer": {
                "ok": True,
                "text": turn.answer,
                "charts": list(turn.charts),
                "sources": list(turn.sources),
                "conversation_id": turn.session_id,
                "trace": dict(turn.trace),
                "elapsed_ms": int(round(elapsed_ms)),
                "file_context_included": False,
            }
        },
        "charts": list(turn.charts),
        "sources": list(turn.sources),
        "conversation_id": turn.session_id,
        "_chat_agent_restored": False,
        "chat_session_title": turn.question[:20] if turn.question else "새로운 채팅",
        "_jw_chat_agent_direct": True,
        **common_markers,
    }
    response_doc = {
        "trace_id": job.trace_id,
        "span_id": job.span_id,
        "data": {"code": 0, "errMsg": "success", "data": rendered},
        **common_markers,
    }
    return service_trace, request_doc, response_doc


class ProjectionProcessor:
    def __init__(
        self,
        session_writer: SessionProjectionWriter,
        mongo_writer: MongoProjectionWriter,
        *,
        pod: str,
        ip: str,
    ) -> None:
        self._session_writer = session_writer
        self._mongo_writer = mongo_writer
        self._pod = pod
        self._ip = ip

    def process(self, job: ProjectionJob) -> None:
        active = self._session_writer.active_service()
        if job.portal_user_id is not None:
            self._session_writer.upsert_hidden(job, active)
        documents = build_projection_documents(job, active, pod=self._pod, ip=self._ip)
        if not self._mongo_writer.upsert_and_verify(job, documents):
            raise RuntimeError("Mongo projection triple verification failed")
        if job.portal_user_id is not None:
            self._session_writer.mark_displayed(job)


@dataclass(frozen=True, slots=True)
class ProjectionDbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


class MySQLProjectionOutbox:
    def __init__(
        self,
        config: ProjectionDbConfig,
        *,
        table_name: str = "jw_chat_agent_history_projection_outbox",
        default_user_id: int | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._config = config
        self._table_name = table_name
        self._default_user_id = default_user_id
        self._max_attempts = max_attempts

    def enqueue(
        self,
        *,
        source_log_id: int,
        session_id: str | None,
        turn_index: int,
        question_text: str,
        answer_text: str,
        charts: Sequence[Mapping[str, Any]],
        sources: Sequence[str],
        trace: Mapping[str, Any],
        timing: Mapping[str, Any],
        projection_context: ProjectionRequestContext | None,
    ) -> None:
        if not session_id:
            LOGGER.warning("history projection skipped: completed turn has no conversation id")
            return
        trace_id = str(trace.get("trace_id") or uuid4())
        turn_id = trace_id
        span_id = uuid4().hex[:16]
        portal_user_id = (
            projection_context.portal_user_id
            if projection_context is not None and projection_context.portal_user_id is not None
            else self._default_user_id
        )
        headers = projection_context.http_headers if projection_context is not None else {}
        payload = {
            "question": question_text,
            "answer": answer_text,
            "charts": [dict(chart) for chart in charts],
            "sources": list(sources),
            "trace": dict(trace),
            "timing": dict(timing),
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._table_name}
                        (source_log_id, session_id, turn_id, turn_index, projection_version,
                         trace_id, span_id, portal_user_id, request_headers_json, payload_json,
                         status, attempts, max_attempts, next_attempt_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 0, %s, NOW())
                    ON DUPLICATE KEY UPDATE id = id
                    """,
                    (
                        source_log_id,
                        session_id,
                        turn_id,
                        turn_index,
                        PROJECTION_VERSION,
                        trace_id,
                        span_id,
                        portal_user_id,
                        _json_dumps(headers),
                        _json_dumps(payload),
                        self._max_attempts,
                    ),
                )
            connection.commit()

    def claim_next(self) -> ProjectionJob | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT * FROM {self._table_name}
                    WHERE (
                        status IN ('pending', 'retry') AND next_attempt_at <= NOW()
                    ) OR (
                        status = 'processing' AND updated_at < DATE_SUB(NOW(), INTERVAL 5 MINUTE)
                    )
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE
                    """
                )
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor.execute(
                    f"UPDATE {self._table_name} SET status='processing', updated_at=NOW() WHERE id=%s",
                    (row["id"],),
                )
            connection.commit()
        return _job_from_row(row)

    def complete(self, outbox_id: int) -> None:
        self._set_status(outbox_id, "completed", None, completed=True)

    def fail(self, job: ProjectionJob, error: Exception) -> None:
        attempts = job.attempts + 1
        status = "dead" if attempts >= self._max_attempts or isinstance(error, ProjectionOwnershipError) else "retry"
        delay = min(300, 2 ** min(attempts, 8))
        message = f"{type(error).__name__}: {error}"[:1000]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._table_name}
                    SET status=%s, attempts=%s, last_error=%s,
                        next_attempt_at=DATE_ADD(NOW(), INTERVAL %s SECOND), updated_at=NOW()
                    WHERE id=%s
                    """,
                    (status, attempts, message, delay, job.outbox_id),
                )
            connection.commit()

    def _set_status(self, outbox_id: int, status: str, error: str | None, *, completed: bool = False) -> None:
        completed_sql = ", completed_at=NOW()" if completed else ""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self._table_name} SET status=%s, last_error=%s, updated_at=NOW(){completed_sql} WHERE id=%s",
                    (status, error, outbox_id),
                )
            connection.commit()

    def _connect(self):
        return _connect_mysql(self._config, dict_cursor=True)


class ProjectionOwnershipError(RuntimeError):
    pass


class MySQLSessionProjectionWriter:
    def __init__(self, config: ProjectionDbConfig, *, endpoint: str, cache_ttl_seconds: int = 60) -> None:
        self._config = config
        self._endpoint = endpoint
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_active: tuple[float, ActiveChatService] | None = None

    def active_service(self) -> ActiveChatService:
        now = time.monotonic()
        if self._cached_active is not None and now - self._cached_active[0] < self._cache_ttl_seconds:
            return self._cached_active[1]
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cs.id AS service_id, csr.id AS revision_id,
                           csp.id AS publication_id, cs.endpoint
                    FROM llmops.chat_service_tb cs
                    JOIN llmops.chat_service_rev_tb csr ON csr.chat_service_id = cs.id
                    JOIN llmops.chat_service_publish_tb csp
                      ON csp.chat_service_id = cs.id
                     AND csp.chat_service_rev_id = csr.id
                     AND csp.del_date IS NULL
                    WHERE cs.endpoint = %s AND cs.is_active = 1
                    ORDER BY csp.id DESC
                    LIMIT 1
                    """,
                    (self._endpoint,),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("active chat service relationship not found")
        active = ActiveChatService(
            service_id=int(row["service_id"]),
            revision_id=int(row["revision_id"]),
            publication_id=int(row["publication_id"]),
            endpoint=str(row["endpoint"]),
        )
        self._cached_active = (now, active)
        return active

    def upsert_hidden(self, job: ProjectionJob, active: ActiveChatService) -> None:
        assert job.portal_user_id is not None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, reg_user_id FROM llmops.chat_session_tb WHERE uid=%s AND is_active=1 FOR UPDATE",
                    (job.turn.session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        INSERT INTO llmops.chat_session_tb
                            (title, chat_service_id, chat_service_rev_id, chat_service_pub_id,
                             reg_user_id, uid, turns, first_user_message, first_user_request,
                             last_user_request, last_bot_response, is_display, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 1)
                        """,
                        (
                            job.turn.question[:20] if job.turn.question else "새로운 채팅",
                            active.service_id,
                            active.revision_id,
                            active.publication_id,
                            job.portal_user_id,
                            job.turn.session_id,
                            job.turn.turn_index,
                            job.turn.question,
                            job.turn.created_at,
                            job.turn.created_at,
                            job.turn.created_at,
                        ),
                    )
                elif int(row["reg_user_id"]) != job.portal_user_id:
                    raise ProjectionOwnershipError("session owner does not match trusted portal user")
                else:
                    cursor.execute(
                        """
                        UPDATE llmops.chat_session_tb
                        SET turns=GREATEST(turns, %s), last_user_request=%s, last_bot_response=%s
                        WHERE id=%s
                        """,
                        (job.turn.turn_index, job.turn.created_at, job.turn.created_at, row["id"]),
                    )
            connection.commit()

    def mark_displayed(self, job: ProjectionJob) -> None:
        assert job.portal_user_id is not None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE llmops.chat_session_tb SET is_display=1
                    WHERE uid=%s AND reg_user_id=%s AND is_active=1
                    """,
                    (job.turn.session_id, job.portal_user_id),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        """
                        SELECT reg_user_id, is_display FROM llmops.chat_session_tb
                        WHERE uid=%s AND is_active=1
                        """,
                        (job.turn.session_id,),
                    )
                    row = cursor.fetchone()
                    if (
                        row is None
                        or int(row["reg_user_id"]) != job.portal_user_id
                        or not bool(row["is_display"])
                    ):
                        raise ProjectionOwnershipError("session display update did not match its trusted owner")
            connection.commit()

    def _connect(self):
        return _connect_mysql(self._config, database="llmops", dict_cursor=True)


class PyMongoProjectionWriter:
    _COLLECTIONS = ("genos_service_trace", "chat-api_request", "chat-api_response")

    def __init__(self, *, host: str, port: int, database: str, username: str, password: str) -> None:
        from pymongo import MongoClient

        self._client = MongoClient(
            host=host,
            port=port,
            username=username,
            password=password,
            authSource=database,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
            socketTimeoutMS=3000,
            retryWrites=True,
        )
        self._database = self._client[database]

    def upsert_and_verify(self, job: ProjectionJob, documents: tuple[dict, dict, dict]) -> bool:
        acknowledgements = []
        for collection_name, document in zip(self._COLLECTIONS, documents, strict=True):
            result = self._database[collection_name].update_one(
                {
                    "trace_id": job.trace_id,
                    "span_id": job.span_id,
                    "origin": PROJECTION_ORIGIN,
                    "history_projection_version": job.projection_version,
                },
                {"$setOnInsert": document},
                upsert=True,
            )
            acknowledgements.append(bool(result.acknowledged))
        return len(acknowledgements) == 3 and all(acknowledgements)


class ProjectionWorker:
    def __init__(self, outbox: MySQLProjectionOutbox, processor: ProjectionProcessor, *, poll_seconds: float = 1.0) -> None:
        self._outbox = outbox
        self._processor = processor
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="history-projection-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._outbox.claim_next()
                if job is None:
                    self._stop.wait(self._poll_seconds)
                    continue
                try:
                    self._processor.process(job)
                except Exception as exc:
                    self._outbox.fail(job, exc)
                    LOGGER.warning(
                        "history projection attempt failed outbox_id=%s attempt=%s error_type=%s",
                        job.outbox_id,
                        job.attempts + 1,
                        type(exc).__name__,
                    )
                else:
                    self._outbox.complete(job.outbox_id)
            except Exception as exc:
                LOGGER.warning("history projection worker poll failed error_type=%s", type(exc).__name__)
                self._stop.wait(self._poll_seconds)


@dataclass(slots=True)
class HistoryProjectionRuntime:
    enabled: bool
    outbox: MySQLProjectionOutbox | None = None
    worker: ProjectionWorker | None = None

    @classmethod
    def from_env(cls) -> "HistoryProjectionRuntime":
        if not _env_bool("HISTORY_PROJECTION_ENABLED", default=False):
            return cls(enabled=False)
        db_config = _projection_db_config_from_env()
        endpoint = os.environ.get("HISTORY_PROJECTION_CHAT_ENDPOINT", "").strip()
        mongo_values = {
            name: os.environ.get(name, "").strip()
            for name in (
                "HISTORY_PROJECTION_MONGO_HOST",
                "HISTORY_PROJECTION_MONGO_PORT",
                "HISTORY_PROJECTION_MONGO_DATABASE",
                "HISTORY_PROJECTION_MONGO_USERNAME",
                "HISTORY_PROJECTION_MONGO_PASSWORD",
            )
        }
        if db_config is None or not endpoint or not all(mongo_values.values()):
            raise RuntimeError("history projection is enabled but required configuration is incomplete")
        default_user_id = _optional_positive_int(os.environ.get("PROJECTION_DEFAULT_USER_ID"))
        outbox = MySQLProjectionOutbox(db_config, default_user_id=default_user_id)
        session_writer = MySQLSessionProjectionWriter(db_config, endpoint=endpoint)
        mongo_writer = PyMongoProjectionWriter(
            host=mongo_values["HISTORY_PROJECTION_MONGO_HOST"],
            port=int(mongo_values["HISTORY_PROJECTION_MONGO_PORT"]),
            database=mongo_values["HISTORY_PROJECTION_MONGO_DATABASE"],
            username=mongo_values["HISTORY_PROJECTION_MONGO_USERNAME"],
            password=mongo_values["HISTORY_PROJECTION_MONGO_PASSWORD"],
        )
        pod, ip = runtime_identity()
        processor = ProjectionProcessor(session_writer, mongo_writer, pod=pod, ip=ip)
        return cls(enabled=True, outbox=outbox, worker=ProjectionWorker(outbox, processor))

    def start(self) -> None:
        if self.worker is not None:
            self.worker.start()

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()


def runtime_identity() -> tuple[str, str]:
    pod = socket.gethostname()
    try:
        ip = socket.gethostbyname(pod)
    except OSError:
        ip = ""
    return pod, ip


def _agent_flow(turn: CompletedTurn) -> list[dict[str, Any]]:
    flow: list[dict[str, Any]] = [
        {
            "nodeId": f"direct-start-{turn.turn_id}",
            "nodeLabel": "질문 접수",
            "data": {
                "id": f"direct-start-{turn.turn_id}",
                "name": "startAgentflow",
                "input": {"question": turn.question},
                "output": {"question": turn.question},
                "state": {},
            },
            "previousNodeIds": [],
            "status": "FINISHED",
        }
    ]
    previous = flow[0]["nodeId"]
    stages = turn.timing.get("stages")
    if isinstance(stages, Sequence) and not isinstance(stages, str | bytes):
        for index, stage in enumerate(stages, start=1):
            if not isinstance(stage, Mapping):
                continue
            node_id = f"direct-stage-{turn.turn_id}-{index}"
            name = str(stage.get("name") or f"step-{index}")
            flow.append(
                {
                    "nodeId": node_id,
                    "nodeLabel": name,
                    "data": {
                        "id": node_id,
                        "name": name,
                        "input": {"question": turn.question},
                        "output": {"elapsed_ms": _number(stage.get("elapsed_ms")) or 0.0},
                        "state": {},
                    },
                    "previousNodeIds": [previous],
                    "status": "FINISHED",
                }
            )
            previous = node_id
    return flow


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _job_from_row(row: Mapping[str, Any]) -> ProjectionJob:
    payload = _json_loads(row["payload_json"])
    created_at = datetime.fromisoformat(str(payload["created_at"]))
    turn = CompletedTurn(
        source_log_id=int(row["source_log_id"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        turn_index=int(row["turn_index"]),
        question=str(payload["question"]),
        answer=str(payload["answer"]),
        charts=tuple(dict(item) for item in payload.get("charts", []) if isinstance(item, Mapping)),
        sources=tuple(str(item) for item in payload.get("sources", [])),
        trace=dict(payload.get("trace", {})),
        timing=dict(payload.get("timing", {})),
        created_at=created_at,
    )
    return ProjectionJob(
        outbox_id=int(row["id"]),
        turn=turn,
        projection_version=int(row["projection_version"]),
        trace_id=str(row["trace_id"]),
        span_id=str(row["span_id"]),
        portal_user_id=int(row["portal_user_id"]) if row.get("portal_user_id") is not None else None,
        request_headers={str(k): str(v) for k, v in _json_loads(row["request_headers_json"]).items()},
        attempts=int(row["attempts"]),
    )


def _connect_mysql(config: ProjectionDbConfig, *, database: str | None = None, dict_cursor: bool = False):
    kwargs: dict[str, Any] = {}
    if dict_cursor:
        kwargs["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=database or config.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
        **kwargs,
    )


def _projection_db_config_from_env() -> ProjectionDbConfig | None:
    values = {
        "host": os.environ.get("CHAT_CACHE_DB_HOST", "").strip(),
        "database": os.environ.get("CHAT_CACHE_DB_NAME", "").strip(),
        "user": os.environ.get("CHAT_CACHE_DB_USER", "").strip(),
        "password": os.environ.get("CHAT_CACHE_DB_PASSWORD", ""),
    }
    if not all(values.values()):
        return None
    return ProjectionDbConfig(
        host=values["host"],
        port=int(os.environ.get("CHAT_CACHE_DB_PORT", "3306")),
        database=values["database"],
        user=values["user"],
        password=values["password"],
    )


def _optional_positive_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError("configured user id must be positive")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_loads(value: object) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}
