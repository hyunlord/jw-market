from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from pipeline.scripts.etl.rnd_trace_adapter import (
    RND_ADAPTER_STATE_DDL,
    RND_CONVERSATION_DDL,
    GenosMonitoringClient,
    MonitoringPayloadError,
    MonitoringRequestError,
    RndTraceAdapter,
    SessionRef,
    build_source_turn_id,
    parse_monitoring_payload,
)


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.statements.append((sql, params))
        if self.connection.fail_upsert and "rnd_trace_conversation_log" in sql and "INSERT" in sql:
            raise RuntimeError("injected upsert failure")

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        self.connection.statements.append((sql, params))
        if self.connection.fail_upsert:
            raise RuntimeError("injected upsert failure")


class FakeConnection:
    def __init__(self, *, fail_upsert: bool = False) -> None:
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_upsert = fail_upsert

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeMonitoringClient:
    def __init__(self, payload: dict[str, object] | Exception) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    def fetch_turns(self, session_ids: list[str]) -> dict[str, object]:
        self.calls.append(session_ids)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _payload() -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "session-1": [
                {
                    "metadata": {
                        "trace_id": "trace-1",
                        "span_id": "span-1",
                        "created_at": "2026-08-03T01:02:03.000Z",
                    },
                    "request": {"data": {"question": "sensitive question"}},
                    "response": {"data": {"data": {"text": "sensitive answer"}}},
                }
            ]
        },
    }


def _sessions() -> list[SessionRef]:
    return [
        SessionRef(
            uid="session-1",
            portal_user_id=34,
            last_user_request=datetime(2026, 8, 3, 1, 3, tzinfo=UTC),
        )
    ]


def test_schema_is_separate_and_has_deterministic_composite_key() -> None:
    ddl = f"{RND_CONVERSATION_DDL}\n{RND_ADAPTER_STATE_DDL}".lower()

    assert "jw_mart`.`rnd_trace_conversation_log" in ddl
    assert "jw_mart`.`rnd_trace_adapter_state" in ddl
    assert "primary key (source_system, service_id, source_turn_id)" in ddl
    assert "jw_chat_agent_conversation_log" not in ddl
    assert "question_text" in ddl and "answer_text" in ddl


def test_source_turn_id_requires_trace_and_span() -> None:
    assert build_source_turn_id("trace-1", "span-1") == "trace-1:span-1"
    with pytest.raises(MonitoringPayloadError, match="trace_id"):
        build_source_turn_id("", "span-1")
    with pytest.raises(MonitoringPayloadError, match="span_id"):
        build_source_turn_id("trace-1", "")


def test_parser_maps_turn_without_emitting_raw_text_in_summary() -> None:
    turns = parse_monitoring_payload(_payload(), {"session-1": _sessions()[0]})

    assert len(turns) == 1
    assert turns[0].source_turn_id == "trace-1:span-1"
    assert turns[0].question_text == "sensitive question"
    assert turns[0].answer_text == "sensitive answer"
    summary = turns[0].safe_summary()
    assert "sensitive question" not in summary
    assert "sensitive answer" not in summary
    assert "question_length=18" in summary
    assert "answer_length=16" in summary


def test_adapter_commits_turns_and_complete_state_together() -> None:
    connection = FakeConnection()
    adapter = RndTraceAdapter(connection, FakeMonitoringClient(_payload()), batch_size=10)

    result = adapter.run(_sessions(), mode="backfill")

    assert result.sessions == 1
    assert result.turns == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "INSERT INTO `jw_mart`.`rnd_trace_conversation_log`" in sql
    assert "status='complete'" in sql


def test_adapter_bounds_monitoring_requests_by_batch_size() -> None:
    payload = {"code": 0, "data": {f"session-{index}": [] for index in range(5)}}
    client = FakeMonitoringClient(payload)
    connection = FakeConnection()
    sessions = [
        SessionRef(
            uid=f"session-{index}",
            portal_user_id=index,
            last_user_request=datetime(2026, 8, 3, 1, index, tzinfo=UTC),
        )
        for index in range(5)
    ]

    RndTraceAdapter(connection, client, batch_size=2).run(sessions, mode="incremental")

    assert [len(call) for call in client.calls] == [2, 2, 1]


def test_adapter_rolls_back_and_records_failed_state_on_monitoring_error() -> None:
    connection = FakeConnection()
    adapter = RndTraceAdapter(
        connection,
        FakeMonitoringClient(TimeoutError("sensitive upstream detail")),
        batch_size=10,
    )

    with pytest.raises(TimeoutError, match="sensitive upstream detail"):
        adapter.run(_sessions(), mode="incremental")

    assert connection.rollbacks == 1
    assert connection.commits == 1
    failure_statements = [
        (sql, params)
        for sql, params in connection.statements
        if "status='failed'" in sql
    ]
    assert len(failure_statements) == 1
    assert "sensitive upstream detail" not in repr(failure_statements)


def test_adapter_rolls_back_partial_upsert_and_records_failure() -> None:
    connection = FakeConnection(fail_upsert=True)
    adapter = RndTraceAdapter(connection, FakeMonitoringClient(_payload()), batch_size=10)

    with pytest.raises(RuntimeError, match="injected upsert failure"):
        adapter.run(_sessions(), mode="backfill")

    assert connection.rollbacks == 1
    assert connection.commits == 1


def test_parser_fails_closed_for_malformed_turn_instead_of_skipping_it() -> None:
    payload = {"code": 0, "data": {"session-1": ["not-an-object"]}}

    with pytest.raises(MonitoringPayloadError, match="not an object"):
        parse_monitoring_payload(payload, {"session-1": _sessions()[0]})


def test_monitoring_http_error_does_not_expose_session_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            "http://monitoring/trace?session_ids=session-secret",
            503,
            "unavailable",
            None,
            None,
        )

    monkeypatch.setattr(
        "pipeline.scripts.etl.rnd_trace_adapter.urlopen",
        fail_request,
    )
    client = GenosMonitoringClient("http://monitoring", request_interval_seconds=0)

    with pytest.raises(MonitoringRequestError) as error:
        client.fetch_turns(["session-secret"])

    assert "session-secret" not in str(error.value)
    assert "503" in str(error.value)


def test_test2_view_unions_rnd_without_exposing_raw_text() -> None:
    sql = Path(
        "deploy/k8s/jw-market/rnd-trace-adapter-test2-view.sql"
    ).read_text(encoding="utf-8").lower()

    assert "dashboard_chat_usage_test2_v" in sql
    assert "jw_chat_agent_conversation_log" in sql
    assert "rnd_trace_conversation_log" in sql
    assert "union all" in sql
    assert "question_text" not in sql
    assert "answer_text" not in sql
    assert "service_id" in sql


def test_cronjob_is_test2_only_bounded_and_non_concurrent() -> None:
    manifest = Path(
        "deploy/k8s/jw-market/rnd-trace-adapter-test2-cronjob.yaml"
    ).read_text(encoding="utf-8")

    assert "name: jw-rnd-trace-adapter-test2" in manifest
    assert 'schedule: "2-57/5 * * * *"' in manifest
    assert "concurrencyPolicy: Forbid" in manifest
    assert 'value: "20"' in manifest
    assert 'value: "168"' in manifest
    assert "jw-rnd-monitoring-api-read-test2.llmops.svc.cluster.local" in manifest
    assert "question" not in manifest.lower()
    assert "answer" not in manifest.lower()


def test_backfill_job_is_separate_and_has_explicit_window() -> None:
    manifest = Path(
        "deploy/k8s/jw-market/rnd-trace-adapter-test2-backfill-job.yaml"
    ).read_text(encoding="utf-8")

    assert "name: jw-rnd-trace-adapter-backfill-test2" in manifest
    assert "- backfill" in manifest
    assert "- --ensure-schema" in manifest
    assert "- --start" in manifest
    assert "- \"2026-07-13\"" in manifest
    assert "restartPolicy: Never" in manifest
    assert "jw-rnd-monitoring-api-read-test2.llmops.svc.cluster.local" in manifest


def test_monitoring_read_service_is_headless_and_test2_only() -> None:
    manifest = Path(
        "deploy/k8s/jw-market/rnd-trace-adapter-test2-monitoring-service.yaml"
    ).read_text(encoding="utf-8")

    assert "name: jw-rnd-monitoring-api-read-test2" in manifest
    assert "clusterIP: None" in manifest
    assert "app: llmops-monitoring-api" in manifest
