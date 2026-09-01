from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
from queue import Empty, Queue
import threading
import time
from typing import Any, Callable, Protocol
from uuid import uuid4

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
from jw_chat_agent_poc.service.latency_instrumentation import get_latency_probe
from jw_chat_agent_poc.tools.query_layer.mart_json import mart_json_default_or_str


LOGGER = logging.getLogger(__name__)


def _log_operational(message: str, *args: object) -> None:
    level = logging.INFO if LOGGER.isEnabledFor(logging.INFO) else logging.WARNING
    LOGGER.log(level, message, *args)


HISTORY_TABLE_NAME = "jw_chat_agent_conversation_log"
_CONVERSATION_SLOTS_TRACE_KEY = "_conversation_slots"
DEFAULT_CONTEXT_TTL_SECONDS = 600
ASYNC_TRACE_PENDING_NOTICE = "조회 상세를 준비하고 있습니다"
ASYNC_TRACE_DEAD_NOTICE = "이 세션의 진단 정보는 저장되지 않았습니다"
ASYNC_TRACE_MAX_ATTEMPTS = 8
ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS = 170.0
ASYNC_TRACE_WRITE_TIMEOUT_SECONDS = 120

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


@dataclass(frozen=True, slots=True)
class PendingTurn:
    """A durable question row reserved before answer generation starts."""

    source_log_id: int
    conversation_id: str
    session_id: str | None
    turn_index: int
    trace_id: str


class ConversationHistoryStore(Protocol):
    def begin_turn(
        self,
        *,
        session_id: str | None,
        conversation_id: str,
        question_text: str,
        projection_context: ProjectionRequestContext | None,
    ) -> PendingTurn | None:
        """Persist an incomplete turn so disconnects cannot erase the question."""

    def complete_turn(
        self,
        pending_turn: PendingTurn,
        *,
        question_text: str,
        answer_text: str,
        trace: Mapping[str, Any],
        timing: Mapping[str, Any],
        sources: Sequence[str],
        charts: Sequence[Mapping[str, Any]],
        projection_context: ProjectionRequestContext | None,
        conversation_slots: ConversationSlots = ConversationSlots(),
    ) -> TurnPersistOutcome:
        """Replace one reserved incomplete turn with its completed answer."""

    def fail_turn(
        self,
        pending_turn: PendingTurn,
        *,
        question_text: str,
        reason: str,
        projection_context: ProjectionRequestContext | None,
    ) -> TurnPersistOutcome:
        """Keep a failed reserved turn visible and explicitly incomplete."""

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

    def load_detail_trace(
        self,
        conversation_id: str,
        response_id: str,
        portal_user_id: int,
    ) -> Mapping[str, Any] | None:
        """Return one user-owned archival trace for an on-demand detail lookup."""

    def resolve_source_conversation_id(self, conversation_id: str) -> str | None:
        """Map a projected restore id to the source id used by upload storage."""


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

    def mark_enqueue_dead(self, *, source_log_id: int) -> None: ...


@dataclass(slots=True)
class TracePersistenceJob:
    source_log_id: int
    session_id: str | None
    conversation_id: str | None
    turn_index: int
    question_text: str
    answer_text: str
    charts: Sequence[Mapping[str, Any]]
    sources: Sequence[str]
    trace: Mapping[str, Any]
    trace_text: str
    timing: Mapping[str, Any]
    projection_context: ProjectionRequestContext | None
    conversation_slots: Mapping[str, Any]
    trace_saved: bool = False


class TracePersistenceScheduler(Protocol):
    def start(self) -> None: ...

    def submit(self, job: TracePersistenceJob) -> None: ...

    def stop(self, *, timeout_seconds: float = ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS) -> bool: ...


class AsyncTracePersistenceWorker:
    """Persist large traces off the response path while retaining FIFO order."""

    def __init__(
        self,
        *,
        persist_trace: Callable[[TracePersistenceJob], None],
        enqueue_projection: Callable[[TracePersistenceJob], None],
        mark_dead: Callable[[TracePersistenceJob, BaseException], None],
        max_attempts: int = ASYNC_TRACE_MAX_ATTEMPTS,
        retry_delays: Sequence[float] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0),
    ) -> None:
        self._persist_trace = persist_trace
        self._enqueue_projection = enqueue_projection
        self._mark_dead = mark_dead
        self._max_attempts = max(1, max_attempts)
        self._retry_delays = tuple(max(0.0, value) for value in retry_delays) or (0.0,)
        self._queue: Queue[TracePersistenceJob] = Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepting = True
        self.attempt_count = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._accepting = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="conversation-trace-persistence-worker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, job: TracePersistenceJob) -> None:
        if not self._accepting:
            raise RuntimeError("trace persistence worker is stopping")
        self._queue.put(job)
        _log_operational(
            "conversation_trace_queued source_log_id=%s queue_length=%s trace_bytes=%s",
            job.source_log_id,
            self._queue.qsize(),
            len(job.trace_text.encode("utf-8")),
        )

    def stop(self, *, timeout_seconds: float = ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        self._accepting = False
        self._stop.set()
        thread = self._thread
        if thread is None:
            return self._queue.empty()
        thread.join(timeout=max(0.0, timeout_seconds))
        flushed = not thread.is_alive() and self._queue.empty()
        log = _log_operational if flushed else LOGGER.error
        log(
            "conversation_trace_shutdown_flush flushed=%s queue_length=%s timeout_seconds=%.3f",
            str(flushed).lower(),
            self._queue.qsize(),
            timeout_seconds,
        )
        return flushed

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                job = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._process(job)
            finally:
                self._queue.task_done()

    def _process(self, job: TracePersistenceJob) -> None:
        last_error: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            self.attempt_count += 1
            started = time.monotonic()
            try:
                if not job.trace_saved:
                    self._persist_trace(job)
                    job.trace_saved = True
                self._enqueue_projection(job)
            except Exception as exc:
                last_error = exc
                LOGGER.warning(
                    "conversation_trace_attempt_failed source_log_id=%s attempt=%s "
                    "max_attempts=%s trace_saved=%s queue_length=%s elapsed_ms=%.3f error_type=%s",
                    job.source_log_id,
                    attempt,
                    self._max_attempts,
                    str(job.trace_saved).lower(),
                    self._queue.qsize(),
                    (time.monotonic() - started) * 1000,
                    type(exc).__name__,
                )
                if attempt < self._max_attempts:
                    delay = self._retry_delays[min(attempt - 1, len(self._retry_delays) - 1)]
                    self._stop.wait(delay)
                    continue
                try:
                    self._mark_dead(job, exc)
                except Exception as dead_exc:
                    LOGGER.exception(
                        "conversation_trace_dead_record_failed source_log_id=%s error_type=%s",
                        job.source_log_id,
                        type(dead_exc).__name__,
                    )
                LOGGER.error(
                    "conversation_trace_dead source_log_id=%s attempts=%s trace_saved=%s error_type=%s",
                    job.source_log_id,
                    self._max_attempts,
                    str(job.trace_saved).lower(),
                    type(exc).__name__,
                )
                return
            _log_operational(
                "conversation_trace_completed source_log_id=%s attempt=%s queue_length=%s elapsed_ms=%.3f",
                job.source_log_id,
                attempt,
                self._queue.qsize(),
                (time.monotonic() - started) * 1000,
            )
            return
        if last_error is not None:
            raise AssertionError("unreachable trace persistence state") from last_error


class MySQLConversationHistoryStore:
    def __init__(
        self,
        config: _DbConfig | None = None,
        *,
        table_name: str = HISTORY_TABLE_NAME,
        projection_outbox: ProjectionOutboxEnqueuer | None = None,
        trace_scheduler: TracePersistenceScheduler | None = None,
        context_ttl_seconds: int = DEFAULT_CONTEXT_TTL_SECONDS,
    ) -> None:
        self._config = config or _db_config_from_env()
        self._table_name = qualified_table_name(table_name, setting="chat history table")
        self._projection_outbox = projection_outbox
        self._context_ttl_seconds = max(1, context_ttl_seconds)
        self._trace_scheduler = trace_scheduler or AsyncTracePersistenceWorker(
            persist_trace=self._persist_trace,
            enqueue_projection=self._enqueue_projection_job,
            mark_dead=self._mark_trace_dead,
            max_attempts=_positive_int_env(
                "CHAT_HISTORY_TRACE_MAX_ATTEMPTS", ASYNC_TRACE_MAX_ATTEMPTS
            ),
        )

    @classmethod
    def from_env(cls) -> "MySQLConversationHistoryStore":
        return cls(table_name=os.environ.get("CHAT_HISTORY_TABLE_NAME", HISTORY_TABLE_NAME).strip())

    def start(self) -> None:
        self._trace_scheduler.start()

    def stop(self) -> bool:
        return self._trace_scheduler.stop(timeout_seconds=_trace_shutdown_timeout_seconds())

    def resolve_source_conversation_id(self, conversation_id: str) -> str | None:
        resolver = getattr(self._projection_outbox, "source_conversation_id", None)
        if not callable(resolver):
            return None
        try:
            return resolver(conversation_id)
        except Exception as exc:  # noqa: BLE001 - upload restore lookup must fail open
            LOGGER.warning(
                "upload_restore_source_id_lookup_failed conversation_id=%s reason=%s",
                conversation_id,
                type(exc).__name__,
            )
            return None

    def begin_turn(
        self,
        *,
        session_id: str | None,
        conversation_id: str,
        question_text: str,
        projection_context: ProjectionRequestContext | None,
    ) -> PendingTurn | None:
        if self._config is None:
            LOGGER.warning("chat history begin skipped: DB config is incomplete")
            return None
        trace_id = uuid4().hex
        trace = {
            "trace_id": trace_id,
            "conversation_status": "incomplete",
            "incomplete_reason": "generating",
        }
        trace_text = encode_trace(_json_dumps(trace))
        try:
            source_log_id, turn_index = self._write_turn(
                conversation_id=conversation_id,
                session_id=session_id,
                question_text=question_text,
                answer_text="생성이 진행 중입니다.",
                tools_called=[],
                sources=(),
                contract_status_value="incomplete",
                quality_label=None,
                elapsed_ms_value=None,
                trace_text=trace_text,
            )
        except Exception:
            LOGGER.exception("failed to persist incomplete chat turn")
            return None
        try:
            self._enqueue_projection(
                source_log_id=source_log_id,
                session_id=session_id,
                conversation_id=conversation_id,
                turn_index=turn_index,
                question_text=question_text,
                answer_text="생성이 진행 중입니다.",
                charts=(),
                sources=(),
                trace=trace,
                timing={},
                projection_context=projection_context,
            )
        except Exception:
            LOGGER.exception(
                "failed to enqueue incomplete chat turn projection source_log_id=%s",
                source_log_id,
            )
        return PendingTurn(
            source_log_id=source_log_id,
            conversation_id=conversation_id,
            session_id=session_id,
            turn_index=turn_index,
            trace_id=trace_id,
        )

    def complete_turn(
        self,
        pending_turn: PendingTurn,
        *,
        question_text: str,
        answer_text: str,
        trace: Mapping[str, Any],
        timing: Mapping[str, Any],
        sources: Sequence[str],
        charts: Sequence[Mapping[str, Any]] = (),
        projection_context: ProjectionRequestContext | None = None,
        conversation_slots: ConversationSlots = ConversationSlots(),
    ) -> TurnPersistOutcome:
        completed_trace = dict(trace)
        completed_trace["trace_id"] = pending_turn.trace_id
        completed_trace["conversation_status"] = "complete"
        completed_trace.pop("incomplete_reason", None)
        quality_taxonomy = completed_trace.get("quality_taxonomy")
        quality_label = (
            _string_value(quality_taxonomy.get("label"))
            if isinstance(quality_taxonomy, Mapping)
            else None
        )
        contract_status = completed_trace.get("answer_contract_status")
        contract_status_value = (
            _string_value(contract_status.get("status"))
            if isinstance(contract_status, Mapping)
            else None
        )
        elapsed_ms = timing.get("total_elapsed_ms")
        elapsed_ms_value = (
            int(round(float(elapsed_ms))) if isinstance(elapsed_ms, int | float) else None
        )
        trace_payload = dict(completed_trace)
        trace_payload[_CONVERSATION_SLOTS_TRACE_KEY] = conversation_slots_to_dict(
            conversation_slots
        )
        trace_text = encode_trace(_json_dumps(trace_payload))
        pending_trace_text = encode_trace(
            _json_dumps(
                {
                    "trace_id": pending_turn.trace_id,
                    "conversation_status": "complete",
                    _CONVERSATION_SLOTS_TRACE_KEY: conversation_slots_to_dict(
                        conversation_slots
                    ),
                    "trace_persistence": {
                        "status": "pending",
                        "notice": ASYNC_TRACE_PENDING_NOTICE,
                    },
                }
            )
        )
        try:
            self._update_reserved_turn(
                source_log_id=pending_turn.source_log_id,
                question_text=question_text,
                answer_text=answer_text,
                tools_called=completed_trace.get("tools_called"),
                sources=sources,
                contract_status_value=contract_status_value,
                quality_label=quality_label,
                elapsed_ms_value=elapsed_ms_value,
                trace_text=pending_trace_text,
            )
        except Exception as exc:
            LOGGER.exception("failed to complete reserved chat history turn")
            return TurnPersistOutcome(
                status=PERSIST_STATUS_FAILED,
                reason="completion_update_error",
                detail=type(exc).__name__,
            )
        job = TracePersistenceJob(
            source_log_id=pending_turn.source_log_id,
            session_id=pending_turn.session_id,
            conversation_id=pending_turn.conversation_id,
            turn_index=pending_turn.turn_index,
            question_text=question_text,
            answer_text=answer_text,
            charts=tuple(charts),
            sources=tuple(sources),
            trace=completed_trace,
            trace_text=trace_text,
            timing=dict(timing),
            projection_context=projection_context,
            conversation_slots=conversation_slots_to_dict(conversation_slots),
        )
        try:
            self._trace_scheduler.submit(job)
        except Exception:
            LOGGER.exception(
                "async completion enqueue failed; using synchronous projection fallback "
                "source_log_id=%s",
                pending_turn.source_log_id,
            )
            try:
                self._persist_trace(job)
                self._enqueue_projection_job(job)
            except Exception as exc:
                LOGGER.exception(
                    "synchronous completion projection fallback failed source_log_id=%s",
                    pending_turn.source_log_id,
                )
                return TurnPersistOutcome(
                    status=PERSIST_STATUS_UNCONFIRMED,
                    reason="completion_projection_error",
                    detail=type(exc).__name__,
                )
        return TurnPersistOutcome(status=PERSIST_STATUS_PERSISTED)

    def fail_turn(
        self,
        pending_turn: PendingTurn,
        *,
        question_text: str,
        reason: str,
        projection_context: ProjectionRequestContext | None,
    ) -> TurnPersistOutcome:
        answer_text = "생성이 중단되었습니다. 다시 질문해 주세요."
        trace = {
            "trace_id": pending_turn.trace_id,
            "conversation_status": "incomplete",
            "incomplete_reason": reason,
        }
        try:
            self._update_reserved_turn(
                source_log_id=pending_turn.source_log_id,
                question_text=question_text,
                answer_text=answer_text,
                tools_called=[],
                sources=(),
                contract_status_value="incomplete",
                quality_label=None,
                elapsed_ms_value=None,
                trace_text=encode_trace(_json_dumps(trace)),
            )
            self._enqueue_projection(
                source_log_id=pending_turn.source_log_id,
                session_id=pending_turn.session_id,
                conversation_id=pending_turn.conversation_id,
                turn_index=pending_turn.turn_index,
                question_text=question_text,
                answer_text=answer_text,
                charts=(),
                sources=(),
                trace=trace,
                timing={},
                projection_context=projection_context,
            )
        except Exception as exc:
            LOGGER.exception(
                "failed to mark reserved chat turn incomplete source_log_id=%s",
                pending_turn.source_log_id,
            )
            return TurnPersistOutcome(
                status=PERSIST_STATUS_FAILED,
                reason="incomplete_update_error",
                detail=type(exc).__name__,
            )
        return TurnPersistOutcome(status=PERSIST_STATUS_PERSISTED)

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
        latency_probe = get_latency_probe(conversation_id)
        trace_payload = dict(trace)
        trace_payload[_CONVERSATION_SLOTS_TRACE_KEY] = conversation_slots_to_dict(conversation_slots)
        serialized_trace = _json_dumps(trace_payload)
        serialized_trace_bytes = len(serialized_trace.encode("utf-8"))
        if latency_probe is not None:
            latency_probe.checkpoint(
                "history.trace_json",
                output_bytes=serialized_trace_bytes,
                object_count=len(trace_payload),
            )
        trace_text = encode_trace(serialized_trace)
        trace_text_bytes = len(trace_text.encode("utf-8"))
        if latency_probe is not None:
            latency_probe.checkpoint(
                "history.trace_encode",
                input_bytes=serialized_trace_bytes,
                output_bytes=trace_text_bytes,
            )
        pending_trace_text = encode_trace(
            _json_dumps(
                {
                    _CONVERSATION_SLOTS_TRACE_KEY: conversation_slots_to_dict(conversation_slots),
                    "trace_persistence": {
                        "status": "pending",
                        "notice": ASYNC_TRACE_PENDING_NOTICE,
                    },
                }
            )
        )
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
                trace_text=pending_trace_text,
            )
            if latency_probe is not None:
                latency_probe.checkpoint(
                    "history.conversation_row",
                    input_bytes=len(pending_trace_text.encode("utf-8")),
                    output_bytes=len(pending_trace_text.encode("utf-8")),
                    fields={"turn_index": turn_index},
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
        self._trace_scheduler.submit(
            TracePersistenceJob(
                source_log_id=source_log_id,
                session_id=session_id,
                conversation_id=conversation_id,
                turn_index=turn_index,
                question_text=question_text,
                answer_text=answer_text,
                charts=tuple(charts),
                sources=tuple(sources),
                trace=dict(trace),
                trace_text=trace_text,
                timing=dict(timing),
                projection_context=projection_context,
                conversation_slots=conversation_slots_to_dict(conversation_slots),
            )
        )
        if latency_probe is not None:
            latency_probe.checkpoint(
                "history.trace_enqueue",
                input_bytes=trace_text_bytes,
                output_bytes=trace_text_bytes,
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
        connect_started = time.monotonic()
        with self._connect() as connection:
            connect_ms = (time.monotonic() - connect_started) * 1000
            index_started = time.monotonic()
            turn_index = self._next_turn_index(connection, conversation_id, session_id)
            index_ms = (time.monotonic() - index_started) * 1000
            with connection.cursor() as cursor:
                insert_started = time.monotonic()
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
                insert_ms = (time.monotonic() - insert_started) * 1000
        _log_operational(
            "conversation_history_statements source_log_id=%s connect_ms=%.3f "
            "turn_index_ms=%.3f insert_ms=%.3f trace_bytes=%s",
            source_log_id,
            connect_ms,
            index_ms,
            insert_ms,
            len(trace_text.encode("utf-8")),
        )
        return source_log_id, turn_index

    def _update_reserved_turn(
        self,
        *,
        source_log_id: int,
        question_text: str,
        answer_text: str,
        tools_called: object,
        sources: Sequence[str],
        contract_status_value: str | None,
        quality_label: str | None,
        elapsed_ms_value: int | None,
        trace_text: str,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {self._table_name}
                SET question_text=%s, answer_text=%s, tools_called=%s, sources=%s,
                    contract_status=%s, quality_label=%s, elapsed_ms=%s, trace_json=%s
                WHERE id=%s
                """,
                (
                    question_text,
                    answer_text,
                    _json_dumps(tools_called if isinstance(tools_called, list) else []),
                    _json_dumps(list(sources)),
                    contract_status_value,
                    quality_label,
                    elapsed_ms_value,
                    trace_text,
                    source_log_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("reserved chat turn was not found")

    def _persist_trace(self, job: TracePersistenceJob) -> None:
        connect_started = time.monotonic()
        with self._connect(read_timeout=ASYNC_TRACE_WRITE_TIMEOUT_SECONDS) as connection:
            connect_ms = (time.monotonic() - connect_started) * 1000
            with connection.cursor() as cursor:
                update_started = time.monotonic()
                cursor.execute(
                    f"UPDATE {self._table_name} SET trace_json = %s WHERE id = %s",
                    (job.trace_text, job.source_log_id),
                )
                update_ms = (time.monotonic() - update_started) * 1000
        _log_operational(
            "conversation_trace_statements source_log_id=%s connect_ms=%.3f "
            "trace_update_ms=%.3f trace_bytes=%s",
            job.source_log_id,
            connect_ms,
            update_ms,
            len(job.trace_text.encode("utf-8")),
        )

    def _enqueue_projection_job(self, job: TracePersistenceJob) -> None:
        started = time.monotonic()
        self._enqueue_projection(
            source_log_id=job.source_log_id,
            session_id=job.session_id,
            conversation_id=job.conversation_id,
            turn_index=job.turn_index,
            question_text=job.question_text,
            answer_text=job.answer_text,
            charts=job.charts,
            sources=job.sources,
            trace=job.trace,
            timing=job.timing,
            projection_context=job.projection_context,
        )
        _log_operational(
            "conversation_projection_enqueue source_log_id=%s elapsed_ms=%.3f",
            job.source_log_id,
            (time.monotonic() - started) * 1000,
        )

    def _mark_trace_dead(self, job: TracePersistenceJob, exc: BaseException) -> None:
        if job.trace_saved:
            mark_enqueue_dead = getattr(self._projection_outbox, "mark_enqueue_dead", None)
            if callable(mark_enqueue_dead):
                mark_enqueue_dead(source_log_id=job.source_log_id)
            else:
                LOGGER.error(
                    "conversation_projection_dead_marker_unavailable source_log_id=%s",
                    job.source_log_id,
                )
            return
        marker = encode_trace(
            _json_dumps(
                {
                    _CONVERSATION_SLOTS_TRACE_KEY: dict(job.conversation_slots),
                    "trace_persistence": {
                        "status": "dead",
                        "notice": ASYNC_TRACE_DEAD_NOTICE,
                    }
                }
            )
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self._table_name} SET trace_json = %s WHERE id = %s",
                (marker, job.source_log_id),
            )
        LOGGER.error(
            "conversation_trace_dead_marker source_log_id=%s error_type=%s",
            job.source_log_id,
            type(exc).__name__,
        )

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
                raise

    def latest_turn(self, conversation_id: str) -> ConversationTurn | None:
        if self._config is None or not conversation_id.strip():
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT question_text, answer_text, trace_json, created_at
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
        created_at = row[3] if len(row) > 3 else None
        trace_payload.setdefault(
            "_history_created_at",
            created_at.isoformat() if created_at else "",
        )
        return ConversationTurn(
            question=str(row[0] or ""),
            answer=str(row[1] or ""),
            slots=conversation_slots_from_dict(trace_payload.get(_CONVERSATION_SLOTS_TRACE_KEY)),
            trace=trace_payload,
        )

    def recent_turns(self, conversation_id: str, limit: int) -> tuple[ConversationTurn, ...]:
        if not conversation_id.strip() or limit <= 0:
            return ()
        if self._config is None:
            raise RuntimeError("conversation history is not configured")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT question_text, answer_text, trace_json, created_at
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
            created_at = row[3] if len(row) > 3 else None
            trace_payload.setdefault(
                "_history_created_at",
                created_at.isoformat() if created_at else "",
            )
            turns.append(
                ConversationTurn(
                    question=str(row[0] or ""),
                    answer=str(row[1] or ""),
                    slots=conversation_slots_from_dict(
                        trace_payload.get(_CONVERSATION_SLOTS_TRACE_KEY)
                    ),
                    trace=trace_payload,
                )
            )
        return tuple(turns)

    def load_detail_trace(
        self,
        conversation_id: str,
        response_id: str,
        portal_user_id: int,
    ) -> Mapping[str, Any] | None:
        if self._config is None or not conversation_id.strip() or not response_id.strip():
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT id, trace_json
                    FROM {self._table_name}
                    WHERE conversation_id = %s
                    ORDER BY turn_index DESC, id DESC
                    LIMIT 50
                    """,
                    (conversation_id,),
                )
                rows = cursor.fetchall()
        for _source_log_id, raw_trace in rows:
            trace = _json_object(raw_trace)
            if str(trace.get("trace_id") or "") != response_id:
                continue
            try:
                owner_id = int(trace.get("_detail_owner_id"))
            except (TypeError, ValueError):
                return None
            return trace if owner_id == portal_user_id else None
        return None

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


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("ignoring unusable %s; keeping %s", name, default)
        return default
    if value <= 0:
        LOGGER.warning("ignoring non-positive %s; keeping %s", name, default)
        return default
    return value


def _trace_shutdown_timeout_seconds() -> float:
    raw = os.environ.get("CHAT_HISTORY_TRACE_SHUTDOWN_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning(
            "ignoring unusable CHAT_HISTORY_TRACE_SHUTDOWN_TIMEOUT_SECONDS; keeping %.1f",
            ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS
    return value if value > 0 else ASYNC_TRACE_SHUTDOWN_TIMEOUT_SECONDS


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
