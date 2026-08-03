from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from pipeline.scripts.etl.refresh_chat_usage_daily import (
    CHAT_DAILY_DDL,
    CHAT_REFRESH_STATE_DDL,
    CHAT_SESSION_DDL,
    ChatUsageRefresh,
    RefreshWindow,
)


class RecordingCursor:
    def __init__(self, *, fail_on: str | tuple[str, ...] | None = None) -> None:
        self.fail_on = fail_on
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.statements.append((sql, params))
        failures = (self.fail_on,) if isinstance(self.fail_on, str) else (self.fail_on or ())
        if any(pattern in sql for pattern in failures):
            raise RuntimeError("injected refresh failure")

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class RecordingConnection:
    def __init__(self, *, fail_on: str | tuple[str, ...] | None = None) -> None:
        self.cursor_instance = RecordingCursor(fail_on=fail_on)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_schema_uses_deterministic_non_null_keys_and_exact_session_rows() -> None:
    ddl = "\n".join((CHAT_DAILY_DDL, CHAT_SESSION_DDL, CHAT_REFRESH_STATE_DDL))

    assert "`jw_mart`.`mart_chat_usage_daily`" in ddl
    assert "`jw_mart`.`mart_chat_usage_daily_session`" in ddl
    assert "`jw_mart`.`mart_chat_usage_refresh_state`" in ddl
    assert "conversation_id VARCHAR(128) NOT NULL" in ddl
    assert "service_key BIGINT NOT NULL" in ddl
    assert "portal_user_key BIGINT NOT NULL" in ddl
    assert "conversation_id" in CHAT_SESSION_DDL
    assert "PRIMARY KEY (usage_date, service_key, portal_user_key, conversation_id)" in ddl


def test_refresh_commits_daily_session_and_state_as_one_transaction() -> None:
    connection = RecordingConnection()
    window = RefreshWindow(date(2026, 8, 1), date(2026, 8, 4))

    ChatUsageRefresh(connection).refresh(window)

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.statements)
    assert "DELETE FROM `jw_mart`.`mart_chat_usage_daily`" in sql
    assert "DELETE FROM `jw_mart`.`mart_chat_usage_daily_session`" in sql
    assert "INSERT INTO `jw_mart`.`mart_chat_usage_daily`" in sql
    assert "INSERT INTO `jw_mart`.`mart_chat_usage_daily_session`" in sql
    assert "INSERT INTO `jw_mart`.`mart_chat_usage_refresh_state`" in sql
    assert "c.conversation_id IS NOT NULL" in sql
    assert "c.conversation_id <> ''" not in sql
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_refresh_rolls_back_all_tables_when_session_insert_fails() -> None:
    connection = RecordingConnection(
        fail_on="INSERT INTO `jw_mart`.`mart_chat_usage_daily_session`"
    )
    window = RefreshWindow(date(2026, 8, 1), date(2026, 8, 4))

    with pytest.raises(RuntimeError, match="injected refresh failure"):
        ChatUsageRefresh(connection).refresh(window)

    failure_sql = "\n".join(
        statement for statement, _ in connection.cursor_instance.statements
    )
    assert "status, last_error" in failure_sql
    assert "refresh_transaction_failed" in failure_sql
    assert connection.commits == 1
    assert connection.rollbacks == 1


def test_refresh_preserves_primary_and_failure_recording_errors() -> None:
    connection = RecordingConnection(
        fail_on=(
            "INSERT INTO `jw_mart`.`mart_chat_usage_daily_session`",
            "refresh_transaction_failed",
        )
    )

    with pytest.raises(ExceptionGroup) as captured:
        ChatUsageRefresh(connection).refresh(
            RefreshWindow(date(2026, 8, 1), date(2026, 8, 4))
        )

    assert len(captured.value.exceptions) == 2
    assert all("injected refresh failure" in str(error) for error in captured.value.exceptions)


def test_test2_refresh_cronjob_is_bounded_and_uses_direct_galera() -> None:
    path = Path("deploy/k8s/jw-market/chat-usage-daily-refresh-test2-cronjob.yaml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    container = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}

    assert document["metadata"]["name"] == "jw-chat-usage-daily-refresh-test2"
    assert document["spec"]["concurrencyPolicy"] == "Forbid"
    assert document["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 1
    assert env["MARIADB_HOST"] == "galera-mariadb-galera.llmops.svc.cluster.local"
    assert env["CHAT_USAGE_REFRESH_DAYS"] == "7"
