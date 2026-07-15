from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jw_chat_agent_poc.service.history_projection import (
    ActiveChatService,
    CompletedTurn,
    MONGO_CONNECT_TIMEOUT_MS,
    MONGO_SERVER_SELECTION_TIMEOUT_MS,
    MONGO_SOCKET_TIMEOUT_MS,
    MySQLProjectionOutbox,
    MySQLSessionProjectionWriter,
    ProjectionDbConfig,
    ProjectionJob,
    ProjectionProcessor,
    ProjectionRequestContext,
    build_projection_documents,
    sanitize_http_headers,
    trusted_portal_user_id,
    _positive_int_env,
)
from jw_chat_agent_poc.service.app import create_app
from jw_chat_agent_poc.service.conversation_history import MySQLConversationHistoryStore, _DbConfig
from jw_chat_agent_poc.service.genos_client import GenosClient


def _turn() -> CompletedTurn:
    return CompletedTurn(
        source_log_id=41,
        session_id="54c0fd4c-0fd5-4ce8-bb47-bb68416cd670",
        turn_id="turn-41",
        turn_index=1,
        question="리바로 시장 현황을 알려줘",
        answer="리바로 시장 현황 답변",
        charts=({"type": "line", "title": "추이"},),
        sources=("UBIST",),
        trace={"tools_called": ["get_metric"]},
        timing={"total_elapsed_ms": 1234, "stages": [{"name": "query", "elapsed_ms": 12.3}]},
        created_at=datetime(2026, 7, 10, 1, 2, 3, tzinfo=UTC),
    )


def _job(*, user_id: int | None = 85) -> ProjectionJob:
    return ProjectionJob(
        outbox_id=7,
        turn=_turn(),
        projection_version=1,
        trace_id="trace-fixed",
        span_id="span-fixed",
        portal_user_id=user_id,
        request_headers={"accept": "text/event-stream", "user-agent": "portal-bff"},
        attempts=0,
    )


def test_portal_user_header_is_trusted_only_after_public_api_key_auth() -> None:
    assert trusted_portal_user_id("85", public_request=True, api_key_authenticated=True) == 85
    assert trusted_portal_user_id("85", public_request=False, api_key_authenticated=False) is None
    assert trusted_portal_user_id("85", public_request=True, api_key_authenticated=False) is None
    assert trusted_portal_user_id(None, public_request=True, api_key_authenticated=True) is None

    with pytest.raises(ValueError, match="positive integer"):
        trusted_portal_user_id("jw-proj-smoke-01", public_request=True, api_key_authenticated=True)


def test_http_header_projection_uses_allowlist_and_drops_credentials() -> None:
    headers = sanitize_http_headers(
        {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "portal-bff",
            "X-Request-Id": "req-1",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-API-Key": "secret-key",
            "X-Portal-User-Id": "85",
        }
    )

    assert headers == {
        "accept": "text/event-stream",
        "content-type": "application/json",
        "user-agent": "portal-bff",
        "x-request-id": "req-1",
    }


def test_golden_projection_shape_restores_question_text_and_agent_flow() -> None:
    docs = build_projection_documents(
        _job(),
        ActiveChatService(service_id=91, revision_id=181, publication_id=838, endpoint="lz0h_sv3e_2qk2"),
        pod="jw-chat-agent-poc-test",
        ip="10.0.0.8",
    )

    service_trace, request_doc, response_doc = docs
    assert service_trace["service"] == "chat-api"
    assert service_trace["name"] == "middleware"
    assert service_trace["http_path"] == "/chat/v2/query/lz0h_sv3e_2qk2"
    assert service_trace["pod"] == "jw-chat-agent-poc-test"
    assert service_trace["ip"] == "10.0.0.8"
    assert service_trace["origin"] == "jw-chat-agent-direct"
    assert service_trace["synthetic_history_projection"] is True
    assert service_trace["billing_eligible"] is False
    assert service_trace["trace_id"] == request_doc["trace_id"] == response_doc["trace_id"] == "trace-fixed"
    assert service_trace["span_id"] == request_doc["span_id"] == response_doc["span_id"] == "span-fixed"
    assert request_doc["data"]["question"] == _turn().question
    assert request_doc["data"]["chatId"] == _turn().session_id
    rendered = response_doc["data"]["data"]
    assert rendered["question"] == _turn().question
    assert rendered["text"] == _turn().answer
    assert rendered["agentFlowExecutedData"]
    assert rendered["_chat_agent_restored"] is False
    assert rendered["_jw_chat_agent_direct"] is True

    assembled = _assemble_like_get_chat_log(service_trace, request_doc, response_doc)
    assert assembled == {
        "question": _turn().question,
        "text": _turn().answer,
        "agentFlowExecutedData": rendered["agentFlowExecutedData"],
    }


def _assemble_like_get_chat_log(service_trace: dict, request_doc: dict, response_doc: dict) -> dict:
    assert service_trace["name"] == "middleware"
    assert service_trace["service"] == "chat-api"
    assert service_trace["http_path"].startswith("/chat/") and "query" in service_trace["http_path"]
    assert (service_trace["trace_id"], service_trace["span_id"]) == (
        request_doc["trace_id"],
        request_doc["span_id"],
    )
    assert (service_trace["trace_id"], service_trace["span_id"]) == (
        response_doc["trace_id"],
        response_doc["span_id"],
    )
    question = request_doc["data"].get("question")
    assert question
    response_data = response_doc["data"]["data"]
    return {
        "question": question,
        "text": response_data["text"],
        "agentFlowExecutedData": response_data["agentFlowExecutedData"],
    }


class _SessionWriter:
    def __init__(self, events: list[str] | None = None) -> None:
        self.displayed = False
        self.upserted = False
        self.events = events

    def active_service(self) -> ActiveChatService:
        return ActiveChatService(91, 181, 838, "lz0h_sv3e_2qk2")

    def upsert_hidden(self, job: ProjectionJob, _active: ActiveChatService) -> None:
        self.upserted = True
        if self.events is not None:
            self.events.append("upsert_hidden")

    def mark_displayed(self, job: ProjectionJob) -> None:
        self.displayed = True
        if self.events is not None:
            self.events.append("mark_displayed")


class _MongoWriter:
    def __init__(self, *, complete: bool = True, events: list[str] | None = None) -> None:
        self.complete = complete
        self.jobs: list[tuple[str, str]] = []
        self.events = events

    def upsert_and_verify(self, job: ProjectionJob, _documents: tuple[dict, dict, dict]) -> bool:
        self.jobs.append((job.trace_id, job.span_id))
        if self.events is not None:
            self.events.append("mongo")
        return self.complete


def test_session_becomes_visible_before_mongo_and_stays_visible_when_projection_fails() -> None:
    events: list[str] = []
    session_writer = _SessionWriter(events)
    mongo_writer = _MongoWriter(complete=False, events=events)
    processor = ProjectionProcessor(session_writer, mongo_writer, pod="pod", ip="10.0.0.1")

    with pytest.raises(RuntimeError, match="triple verification failed"):
        processor.process(_job())

    assert session_writer.upserted is True
    assert session_writer.displayed is True
    assert events == ["upsert_hidden", "mark_displayed", "mongo"]

    mongo_writer.complete = True
    processor.process(_job())
    assert session_writer.displayed is True
    assert mongo_writer.jobs == [("trace-fixed", "span-fixed"), ("trace-fixed", "span-fixed")]


def test_headerless_projection_writes_mongo_without_sidebar_registration() -> None:
    session_writer = _SessionWriter()
    mongo_writer = _MongoWriter()
    ProjectionProcessor(session_writer, mongo_writer, pod="pod", ip="10.0.0.1").process(_job(user_id=None))

    assert session_writer.upserted is False
    assert session_writer.displayed is False
    assert mongo_writer.jobs == [("trace-fixed", "span-fixed")]


def test_retry_keeps_trace_and_span_ids_stable() -> None:
    first = _job()
    retry = replace(first, attempts=first.attempts + 1)

    assert (retry.trace_id, retry.span_id) == (first.trace_id, first.span_id)


def test_mongo_projection_timeouts_remain_bounded_but_allow_slow_upserts() -> None:
    assert MONGO_CONNECT_TIMEOUT_MS == 3000
    assert MONGO_SERVER_SELECTION_TIMEOUT_MS == 3000
    assert MONGO_SOCKET_TIMEOUT_MS == 10000


def test_projection_retry_settings_are_positive_env_values(monkeypatch) -> None:
    monkeypatch.setenv("HISTORY_PROJECTION_MONGO_SOCKET_TIMEOUT_MS", "30000")
    assert _positive_int_env("HISTORY_PROJECTION_MONGO_SOCKET_TIMEOUT_MS", default=10000) == 30000

    monkeypatch.setenv("HISTORY_PROJECTION_MONGO_SOCKET_TIMEOUT_MS", "0")
    with pytest.raises(ValueError, match="positive integer"):
        _positive_int_env("HISTORY_PROJECTION_MONGO_SOCKET_TIMEOUT_MS", default=10000)


class _HistoryStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def record_turn(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("history unavailable")
        self.calls.append(kwargs)


class _Agent:
    def __init__(self, *, external_mode: str = "live") -> None:
        self.external_mode = external_mode

    def answer(self, question: str, _documents=None) -> dict:
        return {"answer": question, "sources": ["cache"], "tool_calls": []}


def test_public_api_key_auth_carries_trusted_portal_user_to_history(monkeypatch) -> None:
    monkeypatch.setenv("DIRECT_ROUTE_API_KEY", "expected-key")
    monkeypatch.setattr(GenosClient, "stream_answer", lambda *_args: iter(("answer",)))
    history = _HistoryStore()
    client = TestClient(create_app(agent_factory=lambda external_mode="live": _Agent(external_mode=external_mode), history_store=history))

    response = client.get(
        "/chat/stream",
        params={"question": "question", "conversation_id": "54c0fd4c-0fd5-4ce8-bb47-bb68416cd670"},
        headers={"host": "jwai-dev.jwhealthcare.com", "x-api-key": "expected-key", "x-portal-user-id": "85"},
    )

    assert response.status_code == 200
    assert history.calls[0]["projection_context"].portal_user_id == 85


def test_internal_request_ignores_portal_user_header(monkeypatch) -> None:
    monkeypatch.setattr(GenosClient, "stream_answer", lambda *_args: iter(("answer",)))
    history = _HistoryStore()
    client = TestClient(create_app(agent_factory=lambda external_mode="live": _Agent(external_mode=external_mode), history_store=history))

    response = client.get(
        "/chat/stream",
        params={"question": "question", "conversation_id": "54c0fd4c-0fd5-4ce8-bb47-bb68416cd670"},
        headers={"host": "testserver", "x-portal-user-id": "85"},
    )

    assert response.status_code == 200
    assert history.calls[0]["projection_context"].portal_user_id is None


def test_history_failure_never_blocks_sse_answer(monkeypatch) -> None:
    monkeypatch.setattr(GenosClient, "stream_answer", lambda *_args: iter(("answer",)))
    client = TestClient(
        create_app(
            agent_factory=lambda external_mode="live": _Agent(external_mode=external_mode),
            history_store=_HistoryStore(fail=True),
        )
    )

    response = client.get("/chat/stream", params={"question": "question"})

    assert response.status_code == 200
    assert "event: done\ndata: ok" in response.text


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple | None]] = []
        self.lastrowid = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append((sql, params))
        if "INSERT INTO jw_chat_agent_conversation_log" in sql:
            self.lastrowid = 41

    def fetchone(self) -> tuple[int]:
        return (1,)


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


class _OutboxEnqueuer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def enqueue(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("outbox unavailable")
        self.calls.append(kwargs)


def test_source_log_commits_before_projection_outbox_enqueue(monkeypatch) -> None:
    connection = _Connection()
    outbox = _OutboxEnqueuer()
    store = MySQLConversationHistoryStore(
        _DbConfig("db", 3306, "jw_mart", "user", "password"),
        projection_outbox=outbox,
    )
    monkeypatch.setattr(store, "_connect", lambda: connection)

    store.record_turn(
        session_id=None,
        conversation_id=_turn().session_id,
        question_text=_turn().question,
        answer_text=_turn().answer,
        trace=_turn().trace,
        timing=_turn().timing,
        sources=_turn().sources,
        charts=_turn().charts,
        projection_context=ProjectionRequestContext(85, {"accept": "text/event-stream"}),
    )

    assert connection.commits == 1
    assert outbox.calls[0]["source_log_id"] == 41
    assert outbox.calls[0]["turn_index"] == 1


def test_projection_outbox_failure_does_not_rollback_source_log(monkeypatch) -> None:
    connection = _Connection()
    store = MySQLConversationHistoryStore(
        _DbConfig("db", 3306, "jw_mart", "user", "password"),
        projection_outbox=_OutboxEnqueuer(fail=True),
    )
    monkeypatch.setattr(store, "_connect", lambda: connection)

    store.record_turn(
        session_id=None,
        conversation_id=_turn().session_id,
        question_text=_turn().question,
        answer_text=_turn().answer,
        trace=_turn().trace,
        timing=_turn().timing,
        sources=_turn().sources,
        charts=_turn().charts,
        projection_context=ProjectionRequestContext(85, {"accept": "text/event-stream"}),
    )

    assert connection.commits == 1
    assert connection.cursor_instance.lastrowid == 41


def test_outbox_enqueue_has_idempotency_guard_and_its_own_commit(monkeypatch) -> None:
    connection = _Connection()
    outbox = MySQLProjectionOutbox(ProjectionDbConfig("db", 3306, "jw_mart", "user", "password"))
    monkeypatch.setattr(outbox, "_connect", lambda: connection)

    outbox.enqueue(
        source_log_id=41,
        session_id=_turn().session_id,
        turn_index=1,
        question_text=_turn().question,
        answer_text=_turn().answer,
        charts=_turn().charts,
        sources=_turn().sources,
        trace=_turn().trace,
        timing=_turn().timing,
        projection_context=ProjectionRequestContext(85, {}),
    )

    statement = connection.cursor_instance.statements[-1][0]
    assert "ON DUPLICATE KEY UPDATE id = id" in statement
    assert connection.commits == 1


def test_outbox_failure_retries_then_dead_letters(monkeypatch) -> None:
    connection = _Connection()
    outbox = MySQLProjectionOutbox(
        ProjectionDbConfig("db", 3306, "jw_mart", "user", "password"),
        max_attempts=2,
    )
    monkeypatch.setattr(outbox, "_connect", lambda: connection)

    outbox.fail(_job(), RuntimeError("temporary"))
    first_params = connection.cursor_instance.statements[-1][1]
    assert first_params is not None and first_params[0] == "retry"

    outbox.fail(replace(_job(), attempts=1), RuntimeError("still unavailable"))
    second_params = connection.cursor_instance.statements[-1][1]
    assert second_params is not None and second_params[0] == "dead"


class _RequeueCursor(_Cursor):
    rowcount = 3


class _RequeueConnection(_Connection):
    def __init__(self) -> None:
        super().__init__()
        self.cursor_instance = _RequeueCursor()


def test_dead_network_timeout_requeue_is_bounded_by_status_error_and_time(monkeypatch) -> None:
    connection = _RequeueConnection()
    outbox = MySQLProjectionOutbox(ProjectionDbConfig("db", 3306, "jw_mart", "user", "password"))
    monkeypatch.setattr(outbox, "_connect", lambda: connection)
    since = datetime(2026, 7, 15, 16, 31)
    until = datetime(2026, 7, 15, 17, 19)

    requeued = outbox.requeue_dead_network_timeouts(since=since, until=until)

    statement, params = connection.cursor_instance.statements[-1]
    assert "status='dead'" in statement
    assert "last_error LIKE 'NetworkTimeout:%%'" in statement
    assert "updated_at >= %s AND updated_at <= %s" in statement
    assert params == (since, until)
    assert requeued == 3
    assert connection.commits == 1


class _DisplayedCursor(_Cursor):
    rowcount = 0

    def fetchone(self):
        return {"reg_user_id": 85, "is_display": 1}


class _DisplayedConnection(_Connection):
    def __init__(self) -> None:
        super().__init__()
        self.cursor_instance = _DisplayedCursor()


def test_display_update_retry_is_idempotent_for_the_same_owner(monkeypatch) -> None:
    connection = _DisplayedConnection()
    writer = MySQLSessionProjectionWriter(
        ProjectionDbConfig("db", 3306, "jw_mart", "user", "password"),
        endpoint="lz0h_sv3e_2qk2",
    )
    monkeypatch.setattr(writer, "_connect", lambda: connection)

    writer.mark_displayed(_job())

    assert connection.commits == 1
    MySQLProjectionOutbox,
    MySQLSessionProjectionWriter,
    ProjectionDbConfig,
