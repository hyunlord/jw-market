from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pymysql

SOURCE_SYSTEM: Final = "genos_monitoring"
RND_SERVICE_ID: Final = 61
DEFAULT_BATCH_SIZE: Final = 20
DEFAULT_OVERLAP_HOURS: Final = 168
DEFAULT_REQUEST_INTERVAL_SECONDS: Final = 1.0

RND_CONVERSATION_DDL: Final = """
CREATE TABLE IF NOT EXISTS `jw_mart`.`rnd_trace_conversation_log` (
    source_system VARCHAR(32) NOT NULL,
    service_id BIGINT UNSIGNED NOT NULL,
    source_turn_id VARCHAR(257) NOT NULL,
    trace_id VARCHAR(128) NOT NULL,
    span_id VARCHAR(128) NOT NULL,
    session_uid VARCHAR(128) NOT NULL,
    portal_user_id BIGINT NULL,
    turn_index INT UNSIGNED NOT NULL,
    question_text LONGTEXT NOT NULL,
    answer_text LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    source_last_user_request DATETIME(6) NOT NULL,
    ingested_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (source_system, service_id, source_turn_id),
    KEY idx_rnd_trace_session (service_id, session_uid, created_at),
    KEY idx_rnd_trace_created (created_at),
    KEY idx_rnd_trace_user_created (portal_user_id, created_at)
) ENGINE=InnoDB
"""

RND_ADAPTER_STATE_DDL: Final = """
CREATE TABLE IF NOT EXISTS `jw_mart`.`rnd_trace_adapter_state` (
    source_system VARCHAR(32) NOT NULL,
    service_id BIGINT UNSIGNED NOT NULL,
    cursor_last_user_request DATETIME(6) NULL,
    last_success_at DATETIME(6) NULL,
    last_attempt_at DATETIME(6) NOT NULL,
    status VARCHAR(16) NOT NULL,
    last_error_code VARCHAR(64) NULL,
    mode VARCHAR(16) NOT NULL,
    session_count BIGINT UNSIGNED NOT NULL,
    turn_count BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (source_system, service_id)
) ENGINE=InnoDB
"""

RND_REJECTION_DDL: Final = """
CREATE TABLE IF NOT EXISTS `jw_mart`.`rnd_trace_adapter_rejection` (
    source_system VARCHAR(32) NOT NULL,
    service_id BIGINT UNSIGNED NOT NULL,
    source_turn_id VARCHAR(257) NOT NULL,
    trace_id VARCHAR(128) NOT NULL,
    span_id VARCHAR(128) NOT NULL,
    session_uid VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    first_seen_at DATETIME(6) NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    occurrence_count BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (source_system, service_id, source_turn_id),
    KEY idx_rnd_rejection_created (created_at),
    KEY idx_rnd_rejection_reason (reason_code, created_at)
) ENGINE=InnoDB
"""

_TURN_UPSERT_SQL: Final = """
INSERT INTO `jw_mart`.`rnd_trace_conversation_log` (
    source_system, service_id, source_turn_id, trace_id, span_id,
    session_uid, portal_user_id, turn_index, question_text, answer_text,
    created_at, source_last_user_request, ingested_at, updated_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
ON DUPLICATE KEY UPDATE
    session_uid=VALUES(session_uid), portal_user_id=VALUES(portal_user_id),
    turn_index=VALUES(turn_index), question_text=VALUES(question_text),
    answer_text=VALUES(answer_text), created_at=VALUES(created_at),
    source_last_user_request=VALUES(source_last_user_request),
    updated_at=UTC_TIMESTAMP(6)
"""

_REJECTION_UPSERT_SQL: Final = """
INSERT INTO `jw_mart`.`rnd_trace_adapter_rejection` (
    source_system, service_id, source_turn_id, trace_id, span_id,
    session_uid, created_at, reason_code, first_seen_at, last_seen_at,
    occurrence_count
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
        UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 1)
ON DUPLICATE KEY UPDATE
    reason_code=VALUES(reason_code), last_seen_at=UTC_TIMESTAMP(6),
    occurrence_count=occurrence_count + 1
"""

_STATE_COMPLETE_SQL: Final = """
INSERT INTO `jw_mart`.`rnd_trace_adapter_state` (
    source_system, service_id, cursor_last_user_request, last_success_at,
    last_attempt_at, status, last_error_code, mode, session_count, turn_count
)
VALUES (%s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 'complete', NULL, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    cursor_last_user_request=GREATEST(
        COALESCE(cursor_last_user_request, VALUES(cursor_last_user_request)),
        COALESCE(VALUES(cursor_last_user_request), cursor_last_user_request)
    ),
    last_success_at=VALUES(last_success_at), last_attempt_at=VALUES(last_attempt_at),
    status='complete', last_error_code=NULL, mode=VALUES(mode),
    session_count=VALUES(session_count), turn_count=VALUES(turn_count)
"""

_STATE_FAILED_SQL: Final = """
INSERT INTO `jw_mart`.`rnd_trace_adapter_state` (
    source_system, service_id, cursor_last_user_request, last_success_at,
    last_attempt_at, status, last_error_code, mode, session_count, turn_count
)
VALUES (%s, %s, NULL, NULL, UTC_TIMESTAMP(6), 'failed', %s, %s, 0, 0)
ON DUPLICATE KEY UPDATE
    last_attempt_at=VALUES(last_attempt_at), status='failed',
    last_error_code=VALUES(last_error_code), mode=VALUES(mode),
    session_count=0, turn_count=0
"""

_SESSION_SELECT_SQL: Final = """
SELECT s.uid,
       CASE WHEN u.id IS NULL THEN NULL ELSE s.reg_user_id END AS portal_user_id,
       s.last_user_request
FROM `llmops`.`chat_session_tb` s
LEFT JOIN `llmops`.`user_tb` u ON u.id = s.reg_user_id
WHERE s.chat_service_id = %s
  AND s.last_user_request >= %s
  AND s.last_user_request < %s
ORDER BY s.last_user_request, s.uid
LIMIT %s
"""

_STATE_CURSOR_SQL: Final = """
SELECT cursor_last_user_request
FROM `jw_mart`.`rnd_trace_adapter_state`
WHERE source_system = %s AND service_id = %s
"""


class MonitoringPayloadError(RuntimeError):
    pass


class MonitoringRequestError(RuntimeError):
    pass


class MonitoringClient(Protocol):
    def fetch_turns(self, session_ids: list[str]) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class SessionRef:
    uid: str
    portal_user_id: int | None
    last_user_request: datetime


@dataclass(frozen=True, slots=True)
class RndTurn:
    source_turn_id: str
    trace_id: str
    span_id: str
    session_uid: str
    portal_user_id: int | None
    turn_index: int
    question_text: str
    answer_text: str
    created_at: datetime
    source_last_user_request: datetime

    def safe_summary(self) -> str:
        return (
            f"source_system={SOURCE_SYSTEM} service_id={RND_SERVICE_ID} "
            f"question_length={len(self.question_text)} "
            f"answer_length={len(self.answer_text)}"
        )

    def as_sql_params(self) -> tuple[object, ...]:
        return (
            SOURCE_SYSTEM,
            RND_SERVICE_ID,
            self.source_turn_id,
            self.trace_id,
            self.span_id,
            self.session_uid,
            self.portal_user_id,
            self.turn_index,
            self.question_text,
            self.answer_text,
            _naive_utc(self.created_at),
            _naive_utc(self.source_last_user_request),
        )


@dataclass(frozen=True, slots=True)
class RejectedTurn:
    source_turn_id: str
    trace_id: str
    span_id: str
    session_uid: str
    created_at: datetime
    reason_code: str

    def as_sql_params(self) -> tuple[object, ...]:
        return (
            SOURCE_SYSTEM,
            RND_SERVICE_ID,
            self.source_turn_id,
            self.trace_id,
            self.span_id,
            self.session_uid,
            _naive_utc(self.created_at),
            self.reason_code,
        )


@dataclass(frozen=True, slots=True)
class ParsedMonitoringPayload:
    turns: list[RndTurn]
    rejections: list[RejectedTurn]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    sessions: int
    turns: int
    rejected_turns: int
    cursor_last_user_request: datetime | None


def build_source_turn_id(trace_id: str, span_id: str) -> str:
    if not trace_id:
        raise MonitoringPayloadError("monitoring turn is missing trace_id")
    if not span_id:
        raise MonitoringPayloadError("monitoring turn is missing span_id")
    return f"{trace_id}:{span_id}"


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise MonitoringPayloadError("monitoring turn is missing created_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MonitoringPayloadError("monitoring turn has invalid created_at") from error
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MonitoringPayloadError(f"monitoring turn is missing {field}")
    return value


def _answer_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        markdown = value.get("markdown")
        if isinstance(markdown, str) and markdown:
            return markdown
    return None


def parse_monitoring_payload(
    payload: dict[str, object],
    sessions: dict[str, SessionRef],
) -> ParsedMonitoringPayload:
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise MonitoringPayloadError("monitoring response envelope is invalid")
    data = payload["data"]
    assert isinstance(data, dict)
    turns: dict[str, RndTurn] = {}
    rejections: dict[str, RejectedTurn] = {}
    for session_uid, session in sessions.items():
        raw_items = data.get(session_uid)
        if not isinstance(raw_items, list):
            raise MonitoringPayloadError("monitoring response is missing a requested session")
        if any(not isinstance(item, dict) for item in raw_items):
            raise MonitoringPayloadError("monitoring turn is not an object")
        ordered_items = sorted(
            raw_items,
            key=lambda item: (
                str((item.get("metadata") or {}).get("created_at", "")),
                str((item.get("metadata") or {}).get("trace_id", "")),
                str((item.get("metadata") or {}).get("span_id", "")),
            ),
        )
        for turn_index, item in enumerate(ordered_items, start=1):
            metadata = item.get("metadata") or {}
            request = item.get("request") or {}
            response = item.get("response") or {}
            if not all(isinstance(value, dict) for value in (metadata, request, response)):
                raise MonitoringPayloadError("monitoring turn sections are invalid")
            trace_id = str(metadata.get("trace_id") or "")
            span_id = str(metadata.get("span_id") or "")
            source_turn_id = build_source_turn_id(trace_id, span_id)
            request_data = request.get("data") or {}
            response_data = response.get("data") or {}
            if not isinstance(request_data, dict) or not isinstance(response_data, dict):
                raise MonitoringPayloadError("monitoring turn data is invalid")
            nested_response = response_data.get("data") or {}
            if not isinstance(nested_response, dict):
                raise MonitoringPayloadError("monitoring response data is invalid")
            created_at = _parse_timestamp(metadata.get("created_at"))
            response_code = response_data.get("code")
            if response_code not in (None, 0, 200) or not nested_response:
                rejections[source_turn_id] = RejectedTurn(
                    source_turn_id=source_turn_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    session_uid=session_uid,
                    created_at=created_at,
                    reason_code="upstream_response_failed",
                )
                continue
            raw_question = request_data.get("question")
            if not isinstance(raw_question, str):
                raw_question = nested_response.get("question")
            question = _required_text(raw_question, "question")
            answer = _answer_text(nested_response.get("text")) or _answer_text(
                nested_response.get("message")
            )
            raw_answer = nested_response.get("text") or nested_response.get("message")
            if answer is None:
                reason_code = "turn_answer_missing"
                if isinstance(raw_answer, dict) and any(
                    key in raw_answer for key in ("error_code", "errMsg", "code")
                ):
                    reason_code = "turn_response_failed"
                rejections[source_turn_id] = RejectedTurn(
                    source_turn_id=source_turn_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    session_uid=session_uid,
                    created_at=created_at,
                    reason_code=reason_code,
                )
                continue
            turn = RndTurn(
                source_turn_id=source_turn_id,
                trace_id=trace_id,
                span_id=span_id,
                session_uid=session_uid,
                portal_user_id=session.portal_user_id,
                turn_index=turn_index,
                question_text=question,
                answer_text=answer,
                created_at=created_at,
                source_last_user_request=session.last_user_request,
            )
            existing = turns.get(source_turn_id)
            if existing is not None and existing != turn:
                raise MonitoringPayloadError("duplicate source turn has conflicting data")
            turns[source_turn_id] = turn
    return ParsedMonitoringPayload(
        turns=sorted(turns.values(), key=lambda turn: (turn.created_at, turn.source_turn_id)),
        rejections=sorted(
            rejections.values(),
            key=lambda rejection: (rejection.created_at, rejection.source_turn_id),
        ),
    )


class GenosMonitoringClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 20.0,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._request_interval_seconds = request_interval_seconds
        self._last_request_at: float | None = None

    def fetch_turns(self, session_ids: list[str]) -> dict[str, object]:
        if not session_ids:
            return {"code": 0, "data": {}}
        self._throttle()
        query = urlencode(
            {
                "service": "chat-api",
                "session_ids": ",".join(session_ids),
                "name": "middleware",
            }
        )
        try:
            with urlopen(
                f"{self._base_url}/trace/chat/detail/list?{query}",
                timeout=self._timeout_seconds,
            ) as response:
                body = response.read()
        except HTTPError as error:
            raise MonitoringRequestError(
                f"monitoring API returned HTTP {error.code}"
            ) from None
        except (TimeoutError, URLError):
            raise MonitoringRequestError("monitoring API request failed") from None
        self._last_request_at = time.monotonic()
        try:
            payload = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            raise MonitoringPayloadError("monitoring API returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise MonitoringPayloadError("monitoring API returned a non-object payload")
        return payload

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self._request_interval_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)


class RndTraceAdapter:
    def __init__(
        self,
        connection: Any,
        monitoring_client: MonitoringClient,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._connection = connection
        self._monitoring_client = monitoring_client
        self._batch_size = batch_size

    def ensure_schema(self) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(RND_CONVERSATION_DDL)
            cursor.execute(RND_REJECTION_DDL)
            cursor.execute(RND_ADAPTER_STATE_DDL)
        self._connection.commit()

    def run(self, sessions: list[SessionRef], *, mode: str) -> AdapterResult:
        if mode not in {"backfill", "incremental"}:
            raise ValueError("mode must be backfill or incremental")
        try:
            turns: list[RndTurn] = []
            rejections: list[RejectedTurn] = []
            for batch in _batches(sessions, self._batch_size):
                by_uid = {session.uid: session for session in batch}
                payload = self._monitoring_client.fetch_turns(list(by_uid))
                parsed = parse_monitoring_payload(payload, by_uid)
                turns.extend(parsed.turns)
                rejections.extend(parsed.rejections)
            cursor_value = max(
                (session.last_user_request for session in sessions),
                default=None,
            )
            with self._connection.cursor() as cursor:
                if turns:
                    cursor.executemany(
                        _TURN_UPSERT_SQL,
                        [turn.as_sql_params() for turn in turns],
                    )
                if rejections:
                    cursor.executemany(
                        _REJECTION_UPSERT_SQL,
                        [rejection.as_sql_params() for rejection in rejections],
                    )
                cursor.execute(
                    _STATE_COMPLETE_SQL,
                    (
                        SOURCE_SYSTEM,
                        RND_SERVICE_ID,
                        _naive_utc(cursor_value) if cursor_value else None,
                        mode,
                        len(sessions),
                        len(turns),
                    ),
                )
            self._connection.commit()
            return AdapterResult(len(sessions), len(turns), len(rejections), cursor_value)
        except Exception as primary_error:
            secondary_errors: list[Exception] = []
            try:
                self._connection.rollback()
            except Exception as rollback_error:
                secondary_errors.append(rollback_error)
            try:
                self._record_failure(mode, _error_code(primary_error))
            except Exception as state_error:
                secondary_errors.append(state_error)
            if secondary_errors:
                raise ExceptionGroup(
                    "RnD trace adapter and failure recording both failed",
                    [primary_error, *secondary_errors],
                )
            raise

    def _record_failure(self, mode: str, error_code: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                _STATE_FAILED_SQL,
                (SOURCE_SYSTEM, RND_SERVICE_ID, error_code, mode),
            )
        self._connection.commit()


def _batches(items: list[SessionRef], size: int) -> Iterable[list[SessionRef]]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _error_code(error: Exception) -> str:
    return type(error).__name__[:64]


def _naive_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).replace(tzinfo=None)


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
        cursorclass=pymysql.cursors.DictCursor,
    )


def _state_cursor(connection: Any) -> datetime | None:
    with connection.cursor() as cursor:
        cursor.execute(_STATE_CURSOR_SQL, (SOURCE_SYSTEM, RND_SERVICE_ID))
        row = cursor.fetchone()
    if not row:
        return None
    return row["cursor_last_user_request"]


def _load_sessions(
    connection: Any,
    start: datetime,
    end_exclusive: datetime,
    limit: int,
) -> list[SessionRef]:
    with connection.cursor() as cursor:
        cursor.execute(
            _SESSION_SELECT_SQL,
            (RND_SERVICE_ID, _naive_utc(start), _naive_utc(end_exclusive), limit + 1),
        )
        rows = cursor.fetchall()
    if len(rows) > limit:
        raise RuntimeError("session query exceeded configured limit")
    return [
        SessionRef(
            uid=str(row["uid"]),
            portal_user_id=row["portal_user_id"],
            last_user_request=row["last_user_request"].replace(tzinfo=UTC),
        )
        for row in rows
    ]


def _window(args: argparse.Namespace, connection: Any) -> tuple[datetime, datetime]:
    end_exclusive = (
        datetime.fromisoformat(args.end_exclusive).replace(tzinfo=UTC)
        if args.end_exclusive
        else datetime.now(UTC)
    )
    if args.mode == "backfill":
        if not args.start:
            raise ValueError("--start is required in backfill mode")
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    else:
        overlap_hours = int(
            os.environ.get("RND_TRACE_OVERLAP_HOURS", str(DEFAULT_OVERLAP_HOURS))
        )
        cursor_value = _state_cursor(connection)
        start = (cursor_value or end_exclusive) - timedelta(hours=overlap_hours)
    if start >= end_exclusive:
        raise ValueError("adapter window start must precede end")
    return start, end_exclusive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project RnD chat traces into a staging table")
    parser.add_argument("--mode", choices=("backfill", "incremental"), required=True)
    parser.add_argument("--start")
    parser.add_argument("--end-exclusive")
    parser.add_argument("--ensure-schema", action="store_true")
    args = parser.parse_args(argv)
    connection = _connect()
    started = datetime.now(UTC)
    try:
        adapter = RndTraceAdapter(
            connection,
            GenosMonitoringClient(
                os.environ.get(
                    "MONITORING_API_BASE_URL",
                    "http://llmops-monitoring-api-service.llmops.svc.cluster.local",
                ),
                timeout_seconds=float(os.environ.get("RND_TRACE_HTTP_TIMEOUT_SECONDS", "20")),
                request_interval_seconds=float(
                    os.environ.get(
                        "RND_TRACE_REQUEST_INTERVAL_SECONDS",
                        str(DEFAULT_REQUEST_INTERVAL_SECONDS),
                    )
                ),
            ),
            batch_size=int(os.environ.get("RND_TRACE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))),
        )
        if args.ensure_schema:
            adapter.ensure_schema()
        start, end_exclusive = _window(args, connection)
        sessions = _load_sessions(
            connection,
            start,
            end_exclusive,
            int(os.environ.get("RND_TRACE_SESSION_LIMIT", "2000")),
        )
        result = adapter.run(sessions, mode=args.mode)
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "event": "rnd_trace_adapter_complete",
                "mode": args.mode,
                "sessions": result.sessions,
                "turns": result.turns,
                "rejected_turns": result.rejected_turns,
                "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
