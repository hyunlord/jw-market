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
from jw_chat_agent_poc.service.trace_codec import decode_trace, encode_trace
from jw_chat_agent_poc.tools.query_layer.mart_json import mart_json_default_or_str


LOGGER = logging.getLogger(__name__)

HISTORY_TABLE_NAME = "jw_chat_agent_conversation_log"
_CONVERSATION_SLOTS_TRACE_KEY = "_conversation_slots"
DEFAULT_CONTEXT_TTL_SECONDS = 600

READ_TIMEOUT_ENV = "CHAT_HISTORY_READ_TIMEOUT_SECONDS"

# How long the answer waits on its own bookkeeping. This is not a data-safety
# limit -- the connection is autocommitting, so a write the server finishes is
# kept whether or not this client is still listening. It is a limit on the
# critical path: the turn is persisted *before* the final answer is streamed, so
# every second spent here is a second the user does not have their answer, and
# the BFF ends the stream at 120 s regardless. Ten seconds leaves room for a
# compressed trace (measured at 1.2 MB for the largest live turn) without
# letting a pathological one hold the answer hostage.
DEFAULT_READ_TIMEOUT_SECONDS = 10

# The verification read after a write timeout. Short on purpose: it answers one
# indexed question about one row, and it must not extend the critical path it
# was added to explain.
_VERIFY_TIMEOUT_SECONDS = 3
_VERIFY_WINDOW_SECONDS = 600

PERSIST_STATUS_PERSISTED = "persisted"
PERSIST_STATUS_SKIPPED = "skipped"
PERSIST_STATUS_UNCONFIRMED = "unconfirmed"
PERSIST_STATUS_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnPersistOutcome:
    """What became of one turn's bookkeeping, in terms a caller can act on.

    ``record_turn`` used to return ``None`` and let the caller's ``except``
    decide, which meant a turn that was never written looked exactly like one
    that was. The user was told nothing either way. This carries the difference
    out so the answer can say so.

    ``unconfirmed`` is deliberately distinct from ``failed``: after a client-side
    timeout the server may well have committed the row, and claiming it was lost
    would be as wrong as claiming it was saved.
    """

    status: str = PERSIST_STATUS_PERSISTED
    reason: str | None = None
    detail: str | None = None

    @property
    def recorded(self) -> bool:
        return self.status in (PERSIST_STATUS_PERSISTED, PERSIST_STATUS_SKIPPED)

    def as_trace(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


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
    ) -> TurnPersistOutcome:
        """Persist a completed chat turn and say what became of it."""

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
    ) -> TurnPersistOutcome:
        if self._config is None:
            LOGGER.warning("chat history persistence skipped: DB config is incomplete")
            return TurnPersistOutcome(status=PERSIST_STATUS_SKIPPED, reason="not_configured")
        quality_taxonomy = trace.get("quality_taxonomy")
        quality_label = _string_value(quality_taxonomy.get("label")) if isinstance(quality_taxonomy, Mapping) else None
        contract_status = trace.get("answer_contract_status")
        contract_status_value = _string_value(contract_status.get("status")) if isinstance(contract_status, Mapping) else None
        elapsed_ms = timing.get("total_elapsed_ms")
        elapsed_ms_value = int(round(float(elapsed_ms))) if isinstance(elapsed_ms, int | float) else None
        tools_called = trace.get("tools_called")
        trace_payload = dict(trace)
        trace_payload[_CONVERSATION_SLOTS_TRACE_KEY] = conversation_slots_to_dict(conversation_slots)
        trace_text = encode_trace(_json_dumps(trace_payload))
        turn_index = 1
        try:
            source_log_id, turn_index = self._write_turn(
                conversation_id=conversation_id,
                session_id=session_id,
                question_text=question_text,
                answer_text=answer_text,
                tools_called=tools_called,
                sources=sources,
                contract_status_value=contract_status_value,
                quality_label=quality_label,
                elapsed_ms_value=elapsed_ms_value,
                trace_text=trace_text,
            )
        except pymysql.err.OperationalError as exc:
            # The write budget ran out. The server may still finish and keep the
            # row -- the connection autocommits precisely so its work is not
            # thrown away -- so ask before deciding what to tell the user.
            outcome = self._outcome_after_write_timeout(exc, conversation_id, session_id)
            LOGGER.warning(
                "chat history write did not confirm conversation_id=%s status=%s error_type=%s",
                conversation_id,
                outcome.status,
                type(exc).__name__,
            )
            return outcome
        self._enqueue_projection(
            source_log_id=source_log_id,
            session_id=session_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            question_text=question_text,
            answer_text=answer_text,
            charts=charts,
            sources=sources,
            trace=trace,
            timing=timing,
            projection_context=projection_context,
        )
        return TurnPersistOutcome(status=PERSIST_STATUS_PERSISTED)

    def _write_turn(
        self,
        *,
        conversation_id: str | None,
        session_id: str | None,
        question_text: str,
        answer_text: str,
        tools_called: object,
        sources: Sequence[str],
        contract_status_value: str | None,
        quality_label: str | None,
        elapsed_ms_value: int | None,
        trace_text: str,
    ) -> tuple[int, int]:
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
                        trace_text,
                    ),
                )
                source_log_id = int(cursor.lastrowid)
        return source_log_id, turn_index

    def _outcome_after_write_timeout(
        self,
        exc: BaseException,
        conversation_id: str | None,
        session_id: str | None,
    ) -> TurnPersistOutcome:
        landed = self._turn_was_written(conversation_id, session_id)
        if landed is True:
            # The server finished after this client stopped waiting, and the row
            # is there. Nothing to tell the user.
            return TurnPersistOutcome(status=PERSIST_STATUS_PERSISTED, reason="confirmed_after_timeout")
        if landed is False:
            return TurnPersistOutcome(
                status=PERSIST_STATUS_UNCONFIRMED,
                reason="write_timeout",
                detail=type(exc).__name__,
            )
        return TurnPersistOutcome(
            status=PERSIST_STATUS_UNCONFIRMED,
            reason="write_timeout_unverifiable",
            detail=type(exc).__name__,
        )

    def _turn_was_written(self, conversation_id: str | None, session_id: str | None) -> bool | None:
        """Did the row land? ``None`` means the question itself could not be asked.

        A separate short-lived connection, because the one that timed out is no
        longer usable. Tri-state on purpose: "we could not check" is not the same
        answer as "it is not there", and collapsing them would put a wrong
        sentence in front of the user.
        """
        if conversation_id:
            where_clause = "conversation_id = %s"
            value = conversation_id
        elif session_id:
            where_clause = "session_id = %s"
            value = session_id
        else:
            return None
        try:
            with self._connect(read_timeout=_VERIFY_TIMEOUT_SECONDS) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT id
                        FROM {self._table_name}
                        WHERE {where_clause}
                          AND created_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (value, _VERIFY_WINDOW_SECONDS),
                    )
                    return cursor.fetchone() is not None
        except Exception:
            LOGGER.exception(
                "chat history write verification failed conversation_id=%s", conversation_id
            )
            return None

    def _enqueue_projection(
        self,
        *,
        source_log_id: int,
        session_id: str | None,
        conversation_id: str | None,
        turn_index: int,
        question_text: str,
        answer_text: str,
        charts: Sequence[Mapping[str, Any]],
        sources: Sequence[str],
        trace: Mapping[str, Any],
        timing: Mapping[str, Any],
        projection_context: ProjectionRequestContext | None,
    ) -> None:
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

    def _connect(self, *, read_timeout: int | None = None):
        assert self._config is not None
        timeout = read_timeout if read_timeout is not None else read_timeout_seconds()
        return pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            charset="utf8mb4",
            # Autocommitting, so that a write the server completes is kept even
            # when this client has already stopped waiting for it. Under the
            # previous explicit transaction the client's timeout closed the
            # connection before COMMIT, and the server discarded work it had
            # measurably spent 11.5 to 50.9 seconds on.
            #
            # This does not widen the turn_index race that already exists: the
            # index is read in its own statement either way, and the previous
            # transaction took no lock on what it read.
            autocommit=True,
            connect_timeout=3,
            read_timeout=timeout,
            write_timeout=timeout,
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


def read_timeout_seconds() -> int:
    raw = os.environ.get(READ_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_READ_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("ignoring unusable %s=%r; keeping %ss", READ_TIMEOUT_ENV, raw, DEFAULT_READ_TIMEOUT_SECONDS)
        return DEFAULT_READ_TIMEOUT_SECONDS
    if value <= 0:
        LOGGER.warning("ignoring non-positive %s=%r; keeping %ss", READ_TIMEOUT_ENV, raw, DEFAULT_READ_TIMEOUT_SECONDS)
        return DEFAULT_READ_TIMEOUT_SECONDS
    return value


def _json_object(value: object) -> dict[str, Any]:
    # Goes through the codec so a compressed row and a row written before the
    # codec existed read back identically. A trace that announces itself
    # compressed and then will not decode raises rather than returning {}:
    # reporting a damaged trace as an absent one would hide the damage.
    parsed = decode_trace(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _string_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
