from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
from typing import Any, Protocol

import pymysql

from jw_chat_agent_poc.service.history_projection import (
    ProjectionEnqueueRecordingError,
    ProjectionRequestContext,
    qualified_table_name,
)
from jw_chat_agent_poc.service.conversation import (
    ConversationSlots,
    ConversationTurn,
    conversation_slots_from_dict,
    conversation_slots_to_dict,
)
from jw_chat_agent_poc.tools.query_layer.mart_json import mart_json_default_or_str


LOGGER = logging.getLogger(__name__)

HISTORY_TABLE_NAME = "jw_chat_agent_conversation_log"
_CONVERSATION_SLOTS_TRACE_KEY = "_conversation_slots"
DEFAULT_CONTEXT_TTL_SECONDS = 600


class ConversationHistoryStore(Protocol):
    def record_turn(
        self,
        *,
        session_id: str | None,
        conversation_id: str | None,
        question_text: str,
        answer_text: str,
        trace: Mapping[str, Any],
        timing: Mapping[str, Any],
        sources: Sequence[str],
        charts: Sequence[Mapping[str, Any]],
        projection_context: ProjectionRequestContext | None,
        conversation_slots: ConversationSlots = ConversationSlots(),
    ) -> None:
        """Persist a completed chat turn."""

    def latest_turn(self, conversation_id: str) -> ConversationTurn | None:
        """Return the latest completed turn for cross-process follow-ups."""

    def recent_turns(self, conversation_id: str, limit: int) -> tuple[ConversationTurn, ...]:
        """Return bounded completed turns for cross-process policy observation."""


@dataclass(frozen=True, slots=True)
class _DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


class ProjectionOutboxEnqueuer(Protocol):
    def enqueue(self, **kwargs: Any) -> None: ...

    def record_enqueue_failure(self, **kwargs: Any) -> None: ...


class MySQLConversationHistoryStore:
    def __init__(
        self,
        config: _DbConfig | None = None,
        *,
        table_name: str = HISTORY_TABLE_NAME,
        projection_outbox: ProjectionOutboxEnqueuer | None = None,
        context_ttl_seconds: int = DEFAULT_CONTEXT_TTL_SECONDS,
    ) -> None:
        self._config = config or _db_config_from_env()
        self._table_name = qualified_table_name(table_name, setting="chat history table")
        self._projection_outbox = projection_outbox
        self._context_ttl_seconds = max(1, context_ttl_seconds)

    @classmethod
    def from_env(cls) -> "MySQLConversationHistoryStore":
        return cls(table_name=os.environ.get("CHAT_HISTORY_TABLE_NAME", HISTORY_TABLE_NAME).strip())

    def record_turn(
        self,
        *,
        session_id: str | None,
        conversation_id: str | None,
        question_text: str,
        answer_text: str,
        trace: Mapping[str, Any],
        timing: Mapping[str, Any],
        sources: Sequence[str],
        charts: Sequence[Mapping[str, Any]] = (),
        projection_context: ProjectionRequestContext | None = None,
        conversation_slots: ConversationSlots = ConversationSlots(),
    ) -> None:
        if self._config is None:
            LOGGER.warning("chat history persistence skipped: DB config is incomplete")
            return
        quality_taxonomy = trace.get("quality_taxonomy")
        quality_label = _string_value(quality_taxonomy.get("label")) if isinstance(quality_taxonomy, Mapping) else None
        contract_status = trace.get("answer_contract_status")
        contract_status_value = _string_value(contract_status.get("status")) if isinstance(contract_status, Mapping) else None
        elapsed_ms = timing.get("total_elapsed_ms")
        elapsed_ms_value = int(round(float(elapsed_ms))) if isinstance(elapsed_ms, int | float) else None
        tools_called = trace.get("tools_called")
        trace_payload = dict(trace)
        trace_payload[_CONVERSATION_SLOTS_TRACE_KEY] = conversation_slots_to_dict(conversation_slots)
        with self._connect() as connection:
            turn_index = self._next_turn_index(connection, conversation_id, session_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._table_name}
                        (
                            conversation_id,
                            session_id,
                            turn_index,
                            question_text,
                            answer_text,
                            tools_called,
                            sources,
                            contract_status,
                            quality_label,
                            elapsed_ms,
                            trace_json
                        )
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        conversation_id,
                        session_id,
                        turn_index,
                        question_text,
                        answer_text,
                        _json_dumps(tools_called if isinstance(tools_called, list) else []),
                        _json_dumps(list(sources)),
                        contract_status_value,
                        quality_label,
                        elapsed_ms_value,
                        _json_dumps(trace_payload),
                    ),
                )
                source_log_id = int(cursor.lastrowid)
            connection.commit()
        if self._projection_outbox is not None:
            try:
                self._projection_outbox.enqueue(
                    source_log_id=source_log_id,
                    session_id=conversation_id or session_id,
                    turn_index=turn_index,
                    question_text=question_text,
                    answer_text=answer_text,
                    charts=charts,
                    sources=sources,
                    trace=trace,
                    timing=timing,
                    projection_context=projection_context,
                )
            except Exception as exc:
                source_kind = projection_context.source_kind if projection_context is not None else "unknown"
                try:
                    self._projection_outbox.record_enqueue_failure(
                        source_log_id=source_log_id,
                        session_id=conversation_id or session_id,
                        source_kind=source_kind,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                except Exception as recording_exc:
                    LOGGER.exception(
                        "history projection enqueue failure ledger write failed "
                        "source_log_id=%s enqueue_error_type=%s recording_error_type=%s",
                        source_log_id,
                        type(exc).__name__,
                        type(recording_exc).__name__,
                    )
                    raise ProjectionEnqueueRecordingError(exc, recording_exc) from recording_exc
                LOGGER.error(
                    "history projection enqueue failed and was recorded "
                    "source_log_id=%s error_type=%s",
                    source_log_id,
                    type(exc).__name__,
                )

    def latest_turn(self, conversation_id: str) -> ConversationTurn | None:
        if self._config is None or not conversation_id.strip():
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT question_text, answer_text, trace_json
                    FROM {self._table_name}
                    WHERE conversation_id = %s
                      AND created_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND
                    ORDER BY turn_index DESC, id DESC
                    LIMIT 1
                    """,
                    (conversation_id, self._context_ttl_seconds),
                )
                row = cursor.fetchone()
        if not row:
            return None
        trace_payload = _json_object(row[2])
        return ConversationTurn(
            question=str(row[0] or ""),
            answer=str(row[1] or ""),
            slots=conversation_slots_from_dict(trace_payload.get(_CONVERSATION_SLOTS_TRACE_KEY)),
        )

    def recent_turns(self, conversation_id: str, limit: int) -> tuple[ConversationTurn, ...]:
        if not conversation_id.strip() or limit <= 0:
            return ()
        if self._config is None:
            raise RuntimeError("conversation history is not configured")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT question_text, answer_text, trace_json
                    FROM {self._table_name}
                    WHERE conversation_id = %s
                      AND created_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND
                    ORDER BY turn_index DESC, id DESC
                    LIMIT %s
                    """,
                    (conversation_id, self._context_ttl_seconds, limit),
                )
                rows = cursor.fetchall()
        turns = []
        for row in reversed(rows):
            trace_payload = _json_object(row[2])
            turns.append(
                ConversationTurn(
                    question=str(row[0] or ""),
                    answer=str(row[1] or ""),
                    slots=conversation_slots_from_dict(
                        trace_payload.get(_CONVERSATION_SLOTS_TRACE_KEY)
                    ),
                )
            )
        return tuple(turns)

    def _connect(self):
        assert self._config is not None
        return pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
        )

    def _next_turn_index(self, connection, conversation_id: str | None, session_id: str | None) -> int:
        if conversation_id:
            where_clause = "conversation_id = %s"
            value = conversation_id
        elif session_id:
            where_clause = "session_id = %s"
            value = session_id
        else:
            return 1
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COALESCE(MAX(turn_index), 0) + 1 FROM {self._table_name} WHERE {where_clause}", (value,))
            row = cursor.fetchone()
        return int(row[0] or 1)


def _db_config_from_env() -> _DbConfig | None:
    host = os.environ.get("CHAT_CACHE_DB_HOST", "").strip()
    database = os.environ.get("CHAT_CACHE_DB_NAME", "").strip()
    user = os.environ.get("CHAT_CACHE_DB_USER", "").strip()
    password = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    port_raw = os.environ.get("CHAT_CACHE_DB_PORT", "3306").strip()
    if not host or not database or not user or not password:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        LOGGER.warning("chat history persistence skipped: invalid CHAT_CACHE_DB_PORT")
        return None
    return _DbConfig(host=host, port=port, database=database, user=user, password=password)


def _json_dumps(value: object) -> str:
    # default= keeps the str() fallback this table has always had, but a mart point now lands as
    # the object the loader read instead of as a repr string that loses ms, rank and unknown keys.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=mart_json_default_or_str,
    )


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _string_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
