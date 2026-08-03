from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import pymysql

DEFAULT_REFRESH_DAYS: Final = 7

CHAT_DAILY_DDL: Final = """
CREATE TABLE IF NOT EXISTS `jw_mart`.`mart_chat_usage_daily` (
    usage_date DATE NOT NULL,
    service_key BIGINT NOT NULL,
    service_id BIGINT NULL,
    portal_user_key BIGINT NOT NULL,
    portal_user_id BIGINT NULL,
    turns BIGINT UNSIGNED NOT NULL,
    attributed_turns BIGINT UNSIGNED NOT NULL,
    total_tokens BIGINT UNSIGNED NOT NULL,
    token_usage_available_turns BIGINT UNSIGNED NOT NULL,
    refreshed_at DATETIME(6) NOT NULL,
    PRIMARY KEY (usage_date, service_key, portal_user_key),
    KEY idx_chat_usage_daily_date (usage_date),
    KEY idx_chat_usage_daily_user_date (portal_user_key, usage_date),
    KEY idx_chat_usage_daily_service_date (service_key, usage_date)
) ENGINE=InnoDB
"""

CHAT_SESSION_DDL: Final = """
CREATE TABLE IF NOT EXISTS `jw_mart`.`mart_chat_usage_daily_session` (
    usage_date DATE NOT NULL,
    service_key BIGINT NOT NULL,
    service_id BIGINT NULL,
    portal_user_key BIGINT NOT NULL,
    portal_user_id BIGINT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    refreshed_at DATETIME(6) NOT NULL,
    PRIMARY KEY (usage_date, service_key, portal_user_key, conversation_id),
    KEY idx_chat_session_date (usage_date),
    KEY idx_chat_session_user_date (portal_user_key, usage_date),
    KEY idx_chat_session_service_date (service_key, usage_date)
) ENGINE=InnoDB
"""

CHAT_REFRESH_STATE_DDL: Final = """
CREATE TABLE IF NOT EXISTS `jw_mart`.`mart_chat_usage_refresh_state` (
    singleton_key TINYINT UNSIGNED NOT NULL,
    coverage_start DATE NOT NULL,
    coverage_end_exclusive DATE NOT NULL,
    last_success_at DATETIME(6) NULL,
    last_attempt_at DATETIME(6) NOT NULL,
    status VARCHAR(16) NOT NULL,
    last_error VARCHAR(128) NULL,
    refreshed_days INT UNSIGNED NOT NULL,
    daily_rows BIGINT UNSIGNED NOT NULL,
    session_rows BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (singleton_key),
    CONSTRAINT chk_chat_refresh_singleton CHECK (singleton_key = 1)
) ENGINE=InnoDB
"""


@dataclass(frozen=True, slots=True)
class RefreshWindow:
    start: date
    end_exclusive: date

    def __post_init__(self) -> None:
        if self.start >= self.end_exclusive:
            raise ValueError("refresh window start must precede end")


class ChatUsageRefresh:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def ensure_schema(self) -> None:
        with self._connection.cursor() as cursor:
            for statement in (CHAT_DAILY_DDL, CHAT_SESSION_DDL, CHAT_REFRESH_STATE_DDL):
                cursor.execute(statement)
        self._connection.commit()

    def refresh(self, window: RefreshWindow) -> None:
        params = (window.start.isoformat(), window.end_exclusive.isoformat())
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM `jw_mart`.`mart_chat_usage_daily` "
                    "WHERE usage_date >= %s AND usage_date < %s",
                    params,
                )
                cursor.execute(
                    "DELETE FROM `jw_mart`.`mart_chat_usage_daily_session` "
                    "WHERE usage_date >= %s AND usage_date < %s",
                    params,
                )
                cursor.execute(_DAILY_INSERT_SQL, params)
                cursor.execute(_SESSION_INSERT_SQL, params)
                cursor.execute(
                    _STATE_UPSERT_SQL,
                    (*params, (window.end_exclusive - window.start).days),
                )
            self._connection.commit()
        except Exception as refresh_error:
            secondary_errors: list[Exception] = []
            try:
                self._connection.rollback()
            except Exception as rollback_error:
                secondary_errors.append(rollback_error)
            try:
                self._record_failure(window)
            except Exception as state_error:
                secondary_errors.append(state_error)
            if secondary_errors:
                raise ExceptionGroup(
                    "chat usage refresh and failure recording both failed",
                    [refresh_error, *secondary_errors],
                )
            raise

    def _record_failure(self, window: RefreshWindow) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                _STATE_FAILURE_UPSERT_SQL,
                (window.start.isoformat(), window.end_exclusive.isoformat()),
            )
        self._connection.commit()


_DAILY_INSERT_SQL: Final = """
INSERT INTO `jw_mart`.`mart_chat_usage_daily` (
    usage_date, service_key, service_id, portal_user_key, portal_user_id,
    turns, attributed_turns, total_tokens, token_usage_available_turns, refreshed_at
)
SELECT DATE(c.created_at), COALESCE(c.service_id, -1), c.service_id,
       COALESCE(c.portal_user_id, -1), c.portal_user_id,
       COUNT(*), SUM(c.portal_user_id IS NOT NULL), SUM(COALESCE(c.total_tokens, 0)),
       SUM(c.token_usage_available = 'true'), UTC_TIMESTAMP(6)
FROM `jw_market_audit_stage`.`dashboard_chat_usage_v` c
WHERE c.created_at >= %s AND c.created_at < %s
GROUP BY DATE(c.created_at), COALESCE(c.service_id, -1), c.service_id,
         COALESCE(c.portal_user_id, -1), c.portal_user_id
"""

_SESSION_INSERT_SQL: Final = """
INSERT INTO `jw_mart`.`mart_chat_usage_daily_session` (
    usage_date, service_key, service_id, portal_user_key, portal_user_id,
    conversation_id, refreshed_at
)
SELECT DISTINCT DATE(c.created_at), COALESCE(c.service_id, -1), c.service_id,
       COALESCE(c.portal_user_id, -1), c.portal_user_id,
       c.conversation_id, UTC_TIMESTAMP(6)
FROM `jw_market_audit_stage`.`dashboard_chat_usage_v` c
WHERE c.created_at >= %s AND c.created_at < %s
  AND c.conversation_id IS NOT NULL AND c.conversation_id <> ''
"""

_STATE_UPSERT_SQL: Final = """
INSERT INTO `jw_mart`.`mart_chat_usage_refresh_state` (
    singleton_key, coverage_start, coverage_end_exclusive, last_success_at,
    last_attempt_at, status, last_error, refreshed_days, daily_rows, session_rows
)
VALUES (
    1, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 'complete', NULL, %s,
    (SELECT COUNT(*) FROM `jw_mart`.`mart_chat_usage_daily`),
    (SELECT COUNT(*) FROM `jw_mart`.`mart_chat_usage_daily_session`)
)
ON DUPLICATE KEY UPDATE
    coverage_start=LEAST(coverage_start, VALUES(coverage_start)),
    coverage_end_exclusive=GREATEST(coverage_end_exclusive, VALUES(coverage_end_exclusive)),
    last_success_at=VALUES(last_success_at), last_attempt_at=VALUES(last_attempt_at),
    status='complete', last_error=NULL,
    refreshed_days=VALUES(refreshed_days), daily_rows=VALUES(daily_rows),
    session_rows=VALUES(session_rows)
"""

_STATE_FAILURE_UPSERT_SQL: Final = """
INSERT INTO `jw_mart`.`mart_chat_usage_refresh_state` (
    singleton_key, coverage_start, coverage_end_exclusive, last_success_at,
    last_attempt_at, status, last_error, refreshed_days, daily_rows, session_rows
)
VALUES (1, %s, %s, NULL, UTC_TIMESTAMP(6), 'failed', 'refresh_transaction_failed', 0, 0, 0)
ON DUPLICATE KEY UPDATE
    last_attempt_at=VALUES(last_attempt_at), status='failed',
    last_error=VALUES(last_error)
"""


def _connect() -> Any:
    password = os.environ.get("MARIADB_ROOT_PASSWORD")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD is required")
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "galera-mariadb-galera.llmops.svc.cluster.local"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=password,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
        read_timeout=120,
        write_timeout=120,
    )


def _window(args: argparse.Namespace) -> RefreshWindow:
    end_exclusive = (
        date.fromisoformat(args.end_exclusive)
        if args.end_exclusive
        else datetime.now(UTC).date() + timedelta(days=1)
    )
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        days = int(os.environ.get("CHAT_USAGE_REFRESH_DAYS", str(DEFAULT_REFRESH_DAYS)))
        start = end_exclusive - timedelta(days=days)
    return RefreshWindow(start, end_exclusive)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh dashboard chat daily materialization")
    parser.add_argument("--start")
    parser.add_argument("--end-exclusive")
    parser.add_argument("--ensure-schema", action="store_true")
    args = parser.parse_args(argv)
    window = _window(args)
    connection = _connect()
    started = datetime.now(UTC)
    try:
        refresh = ChatUsageRefresh(connection)
        if args.ensure_schema:
            refresh.ensure_schema()
        refresh.refresh(window)
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "event": "chat_usage_daily_refresh_complete",
                "start": window.start.isoformat(),
                "end_exclusive": window.end_exclusive.isoformat(),
                "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
