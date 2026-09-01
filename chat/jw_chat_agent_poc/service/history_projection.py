from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4, uuid5

import pymysql

from jw_chat_agent_poc.service.sse_protocol import iter_markdown_sse_events
from jw_chat_agent_poc.service.trace_transport import project_answer_for_transport
from jw_chat_agent_poc.tools.query_layer.mart_json import mart_json_default_or_str

PROJECTION_ORIGIN = "jw-chat-agent-direct"
PROJECTION_VERSION = 2
OUTBOX_PENDING_STATUS = "pending_v2"
OUTBOX_RETRY_STATUS = "retry_v2"
OUTBOX_PROCESSING_STATUS = "processing_v2"
MONGO_SERVER_SELECTION_TIMEOUT_MS = 3000
MONGO_CONNECT_TIMEOUT_MS = 3000
MONGO_SOCKET_TIMEOUT_MS = 10000
MONGO_BSON_MAX_BYTES = 16 * 1024 * 1024
HISTORY_PROJECTION_BSON_SAFETY_MARGIN_BYTES = 1024 * 1024
HISTORY_PROJECTION_BSON_BUDGET_BYTES = (
    MONGO_BSON_MAX_BYTES - HISTORY_PROJECTION_BSON_SAFETY_MARGIN_BYTES
)
HISTORY_PROJECTION_TRACE_BUDGET_BYTES = 8 * 1024 * 1024
HISTORY_PROJECTION_REFERENCE_TEXT_BYTES = 64 * 1024
HISTORY_REPLAY_PARTIAL_NOTICE = (
    "재진입에서 일부 진단 정보를 표시하지 못합니다. "
    "전체 기록은 저장 기록에 보존되어 있습니다."
)
RESTORE_CONTRACT_FIELDS = (
    "answer_sections",
    "evidence_catalog",
    "structured_tables",
    "temp_documents",
    "sourceDocuments",
    "restore_partial",
    "history_projection",
)
UPLOAD_RESTORE_FIELDS = ("temp_documents", "sourceDocuments")
TABLE_SNAPSHOT_FIELDS = (
    "table_id",
    "title",
    "caption",
    "source_label",
    "columns",
    "rows",
    "row_count",
    "total_rows",
    "truncated",
    "unit",
    "source_lane",
    "omitted_columns",
)
LOGGER = logging.getLogger(__name__)
PROJECTION_SESSION_NAMESPACE = UUID("b1f492b7-fc15-5ec8-ae84-5e6e4532c6d8")
SourceKind = Literal[
    "portal_user",
    "synthetic_test",
    "internal_system",
    "anonymous_direct",
    "unknown",
]
SOURCE_KINDS = frozenset(
    {"portal_user", "synthetic_test", "internal_system", "anonymous_direct", "unknown"}
)
_QUALIFIED_TABLE_NAME = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?$")
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


def qualified_table_name(value: str, *, setting: str) -> str:
    if not _QUALIFIED_TABLE_NAME.fullmatch(value):
        raise ValueError(f"{setting} must be an unquoted table name with an optional schema")
    return value


@dataclass(frozen=True, slots=True)
class ProjectionRequestContext:
    portal_user_id: int | None
    http_headers: dict[str, str]
    source_kind: SourceKind = "unknown"


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
    source_conversation_id: str | None = None
    source_kind: SourceKind = "unknown"


class ProjectionEnqueueRecordingError(RuntimeError):
    def __init__(self, enqueue_error: Exception, recording_error: Exception) -> None:
        super().__init__(
            "projection enqueue failed and its failure ledger could not be written: "
            f"{type(enqueue_error).__name__}; {type(recording_error).__name__}"
        )
        self.enqueue_error_type = type(enqueue_error).__name__
        self.recording_error_type = type(recording_error).__name__


def projection_session_id(source_session_id: str) -> str:
    if not source_session_id:
        raise ValueError("projection source session id must not be empty")
    if len(source_session_id) <= 36:
        return source_session_id
    return str(uuid5(PROJECTION_SESSION_NAMESPACE, source_session_id))


def projection_source_kind(
    *,
    public_request: bool | None,
    portal_user_id: int | None,
    synthetic_test: bool = False,
) -> SourceKind:
    if synthetic_test:
        return "synthetic_test"
    if public_request is True:
        return "portal_user" if portal_user_id is not None else "anonymous_direct"
    if public_request is False:
        return "internal_system"
    return "unknown"


def source_kind_from_value(value: object) -> SourceKind:
    normalized = str(value or "unknown")
    if normalized in SOURCE_KINDS:
        return cast(SourceKind, normalized)
    return "unknown"


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
    payload_json: str = ""


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


def _build_projection_documents_unbounded(
    job: ProjectionJob,
    active: ActiveChatService,
    *,
    pod: str,
    ip: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    turn = job.turn
    elapsed_ms = _number(turn.timing.get("total_elapsed_ms")) or 0.0
    source_conversation_id = turn.source_conversation_id or turn.session_id
    common_markers = {
        "origin": PROJECTION_ORIGIN,
        "synthetic_history_projection": True,
        "history_projection_version": job.projection_version,
        "source_kind": turn.source_kind,
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
                "conversation_id": source_conversation_id,
                "trace": dict(turn.trace),
                "elapsed_ms": int(round(elapsed_ms)),
                "file_context_included": False,
            }
        },
        "charts": list(turn.charts),
        "sources": list(turn.sources),
        "conversation_id": source_conversation_id,
        "_chat_agent_restored": False,
        "chat_session_title": turn.question[:20] if turn.question else "새로운 채팅",
        "_jw_chat_agent_direct": True,
        **common_markers,
    }
    rendered.update(_restore_surface_fields(turn.trace, answer=turn.answer))
    if "conversation_status" in turn.trace:
        rendered["conversation_status"] = str(turn.trace["conversation_status"])
    response_doc = {
        "trace_id": job.trace_id,
        "span_id": job.span_id,
        "data": {"code": 0, "errMsg": "success", "data": rendered},
        **common_markers,
    }
    return service_trace, request_doc, response_doc


def _restore_surface_fields(
    trace: Mapping[str, Any], *, answer: str = ""
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    answer_sections = trace.get("answer_sections")
    if isinstance(answer_sections, Mapping):
        sections = answer_sections.get("sections")
        paragraphs = answer_sections.get("paragraphs")
        if (
            answer_sections.get("schema") == "jw.answer-sections.v1"
            and isinstance(sections, Sequence)
            and not isinstance(sections, (str, bytes))
            and isinstance(paragraphs, Mapping)
        ):
            fields["answer_sections"] = {
                "schema": "jw.answer-sections.v1",
                "sections": deepcopy(list(sections)),
                "paragraphs": deepcopy(dict(paragraphs)),
            }
            evidence_catalog = answer_sections.get("evidence_catalog")
            if isinstance(evidence_catalog, Mapping):
                fields["evidence_catalog"] = deepcopy(dict(evidence_catalog))

    structured_tables = trace.get("structured_tables")
    if not isinstance(structured_tables, Sequence) or isinstance(
        structured_tables, (str, bytes)
    ):
        structured_tables = trace.get("tables")
    if (
        not isinstance(structured_tables, Sequence)
        or isinstance(structured_tables, (str, bytes))
        or not structured_tables
    ) and answer:
        structured_tables = _structured_tables_from_live_markdown(answer)
    if isinstance(structured_tables, Sequence) and not isinstance(
        structured_tables, (str, bytes)
    ) and structured_tables:
        fields["structured_tables"] = [
            {
                field: deepcopy(table[field])
                for field in TABLE_SNAPSHOT_FIELDS
                if field in table
            }
            for table in structured_tables
            if isinstance(table, Mapping)
        ]
    for key, uploads in _nested_upload_lists(trace).items():
        fields[key] = deepcopy(uploads)
    return fields


def _nested_upload_lists(trace: Mapping[str, Any]) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}

    def visit(value: object) -> None:
        if len(found) == len(UPLOAD_RESTORE_FIELDS):
            return
        if isinstance(value, Mapping):
            for key in UPLOAD_RESTORE_FIELDS:
                candidate = value.get(key)
                if (
                    key not in found
                    and isinstance(candidate, Sequence)
                    and not isinstance(candidate, (str, bytes))
                ):
                    found[key] = list(candidate)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested in value:
                visit(nested)

    visit(trace)
    return found


def _structured_tables_from_live_markdown(answer: str) -> list[dict[str, Any]]:
    if "|" not in answer or "---" not in answer:
        return []
    latest: list[dict[str, Any]] = []
    for event in iter_markdown_sse_events(answer):
        if not event.startswith("event: tables\n"):
            continue
        data = "\n".join(
            line.removeprefix("data: ")
            for line in event.splitlines()
            if line.startswith("data: ")
        )
        payload = json.loads(data)
        if isinstance(payload, list):
            latest = [dict(table) for table in payload if isinstance(table, Mapping)]
    return latest


def _response_projection_update(document: dict[str, Any]) -> dict[str, Any]:
    rendered = document.get("data", {}).get("data", {})
    if not isinstance(rendered, Mapping):
        return {"$setOnInsert": deepcopy(document)}

    mutable_fields = RESTORE_CONTRACT_FIELDS
    if "conversation_status" in rendered:
        mutable_fields = (
            *mutable_fields,
            "text",
            "genos_persist",
            "charts",
            "sources",
            "conversation_status",
        )
    surface_update = {
        f"data.data.{field}": deepcopy(rendered[field])
        for field in mutable_fields
        if field in rendered
    }
    if not surface_update:
        return {"$setOnInsert": deepcopy(document)}

    insert_fields = {
        field: deepcopy(value)
        for field, value in document.items()
        if field != "data"
    }
    data = document.get("data")
    if isinstance(data, Mapping):
        insert_fields.update(
            {
                f"data.{field}": deepcopy(value)
                for field, value in data.items()
                if field != "data"
            }
        )
    insert_fields.update(
        {
            f"data.data.{field}": deepcopy(value)
            for field, value in rendered.items()
            if field not in mutable_fields
        }
    )
    return {"$setOnInsert": insert_fields, "$set": surface_update}


def projection_bson_sizes(
    documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
) -> dict[str, int]:
    from bson import BSON

    names = ("genos_service_trace", "chat-api_request", "chat-api_response")
    return {
        name: len(BSON.encode(document))
        for name, document in zip(names, documents, strict=True)
    }


def build_projection_documents(
    job: ProjectionJob,
    active: ActiveChatService,
    *,
    pod: str,
    ip: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    documents = _build_projection_documents_unbounded(job, active, pod=pod, ip=ip)
    original_sizes = projection_bson_sizes(documents)
    if max(original_sizes.values()) <= HISTORY_PROJECTION_BSON_BUDGET_BYTES:
        _log_projection_size(
            job,
            original_sizes=original_sizes,
            projected_sizes=original_sizes,
            partial=False,
            selection_method="none",
        )
        return documents

    trace_budget = HISTORY_PROJECTION_TRACE_BUDGET_BYTES
    projected_answer = project_answer_for_transport(
        job.turn.answer,
        job.turn.trace,
        budget_bytes=trace_budget,
    )
    projected_job = replace(
        job,
        turn=replace(
            job.turn,
            answer=_append_partial_replay_notice(projected_answer.text),
            trace=projected_answer.trace,
        ),
    )
    projected_documents = _build_projection_documents_unbounded(
        projected_job,
        active,
        pod=pod,
        ip=ip,
    )
    projected_sizes = projection_bson_sizes(projected_documents)
    while max(projected_sizes.values()) > HISTORY_PROJECTION_BSON_BUDGET_BYTES and trace_budget > 64 * 1024:
        trace_budget //= 2
        projected_answer = project_answer_for_transport(
            job.turn.answer,
            job.turn.trace,
            budget_bytes=trace_budget,
        )
        projected_job = replace(
            job,
            turn=replace(
                job.turn,
                answer=_append_partial_replay_notice(projected_answer.text),
                trace=projected_answer.trace,
            ),
        )
        projected_documents = _build_projection_documents_unbounded(
            projected_job,
            active,
            pod=pod,
            ip=ip,
        )
        projected_sizes = projection_bson_sizes(projected_documents)

    if max(projected_sizes.values()) > HISTORY_PROJECTION_BSON_BUDGET_BYTES:
        projected_trace = {
            "response_size": {
                "archive_reference": "conversation_trace_json",
                "budget_exceeded": True,
                "notice": HISTORY_REPLAY_PARTIAL_NOTICE,
                "selection_method": "stored_record_reference",
            }
        }
        projected_job = replace(
            job,
            turn=replace(
                job.turn,
                answer=_append_partial_replay_notice(job.turn.answer),
                trace=projected_trace,
            ),
        )
        projected_documents = _build_projection_documents_unbounded(
            projected_job,
            active,
            pod=pod,
            ip=ip,
        )
        projected_sizes = projection_bson_sizes(projected_documents)
        if max(projected_sizes.values()) > HISTORY_PROJECTION_BSON_BUDGET_BYTES:
            projected_documents = _fit_reference_only_projection(
                job,
                active,
                pod=pod,
                ip=ip,
                projected_trace=projected_trace,
            )
            projected_sizes = projection_bson_sizes(projected_documents)

    rendered = projected_documents[2]["data"]["data"]
    response_size = rendered["genos_persist"]["chat_agent_answer"]["trace"].get("response_size")
    selection_method = (
        str(response_size.get("selection_method") or "upstream_order")
        if isinstance(response_size, Mapping)
        else "upstream_order"
    )
    original_restore_fields = _restore_surface_fields(
        job.turn.trace, answer=job.turn.answer
    )
    omitted_restore_fields = tuple(
        field for field in original_restore_fields if field not in rendered
    )
    _attach_history_projection_metadata(
        projected_documents,
        selection_method=selection_method,
        omitted_fields=omitted_restore_fields,
    )
    projected_sizes = projection_bson_sizes(projected_documents)
    if max(projected_sizes.values()) > HISTORY_PROJECTION_BSON_BUDGET_BYTES:
        raise RuntimeError("bounded history projection still exceeds the configured BSON budget")
    _log_projection_size(
        job,
        original_sizes=original_sizes,
        projected_sizes=projected_sizes,
        partial=True,
        selection_method=selection_method,
    )
    return projected_documents


def _append_partial_replay_notice(text: str) -> str:
    if HISTORY_REPLAY_PARTIAL_NOTICE in text:
        return text
    return (
        f"{text.rstrip()}\n\n> {HISTORY_REPLAY_PARTIAL_NOTICE}"
        if text.strip()
        else HISTORY_REPLAY_PARTIAL_NOTICE
    )


def _fit_reference_only_projection(
    job: ProjectionJob,
    active: ActiveChatService,
    *,
    pod: str,
    ip: str,
    projected_trace: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    restore_omitted_fields = tuple(
        _restore_surface_fields(job.turn.trace, answer=job.turn.answer)
    )
    bounded_question = _utf8_prefix(
        job.turn.question,
        HISTORY_PROJECTION_REFERENCE_TEXT_BYTES,
    )
    bounded_job = replace(
        job,
        turn=replace(
            job.turn,
            question=bounded_question,
            charts=(),
            sources=(),
            trace=projected_trace,
            timing={"total_elapsed_ms": job.turn.timing.get("total_elapsed_ms", 0)},
        ),
    )
    low = 0
    high = len(job.turn.answer)
    best: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    while low <= high:
        midpoint = (low + high) // 2
        candidate_job = replace(
            bounded_job,
            turn=replace(
                bounded_job.turn,
                answer=_append_partial_replay_notice(job.turn.answer[:midpoint]),
            ),
        )
        candidate = _build_projection_documents_unbounded(
            candidate_job,
            active,
            pod=pod,
            ip=ip,
        )
        _attach_history_projection_metadata(
            candidate,
            selection_method="stored_record_reference",
            omitted_fields=(
                "answer_tail",
                "charts",
                "sources",
                "timing_detail",
                *restore_omitted_fields,
                *(('question_tail',) if bounded_question != job.turn.question else ()),
            ),
        )
        if max(projection_bson_sizes(candidate).values()) <= HISTORY_PROJECTION_BSON_BUDGET_BYTES:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    if best is None:
        raise RuntimeError("history projection fixed fields exceed the configured BSON budget")
    return best


def _attach_history_projection_metadata(
    documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    *,
    selection_method: str,
    omitted_fields: tuple[str, ...] = (),
) -> None:
    rendered = documents[2]["data"]["data"]
    current = rendered.get("history_projection")
    metadata: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    metadata.update({
        "partial": True,
        "archive_reference": "conversation_trace_json",
        "selection_method": selection_method,
    })
    existing_omitted = metadata.get("omitted_fields")
    merged_omitted = list(existing_omitted) if isinstance(existing_omitted, list) else []
    merged_omitted.extend(field for field in omitted_fields if field not in merged_omitted)
    if merged_omitted:
        metadata["omitted_fields"] = merged_omitted
    rendered["history_projection"] = metadata
    rendered["restore_partial"] = {
        "partial": True,
        "archive_reference": "conversation_trace_json",
        "selection_method": selection_method,
        "omitted_fields": merged_omitted,
    }


def _utf8_prefix(value: str, budget_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= budget_bytes:
        return value
    return encoded[:budget_bytes].decode("utf-8", errors="ignore")


def _log_projection_size(
    job: ProjectionJob,
    *,
    original_sizes: Mapping[str, int],
    projected_sizes: Mapping[str, int],
    partial: bool,
    selection_method: str,
) -> None:
    level = logging.INFO if LOGGER.isEnabledFor(logging.INFO) else logging.WARNING
    LOGGER.log(
        level,
        "history_projection_size outbox_id=%s trace_id=%s original_bson_bytes=%s "
        "projected_bson_bytes=%s budget_bytes=%d safety_margin_bytes=%d "
        "partial=%s selection_method=%s retry_attempt=%d dead_transition=false",
        job.outbox_id,
        job.trace_id,
        json.dumps(dict(original_sizes), sort_keys=True, separators=(",", ":")),
        json.dumps(dict(projected_sizes), sort_keys=True, separators=(",", ":")),
        HISTORY_PROJECTION_BSON_BUDGET_BYTES,
        HISTORY_PROJECTION_BSON_SAFETY_MARGIN_BYTES,
        str(partial).lower(),
        selection_method,
        job.attempts,
    )


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
            self._session_writer.mark_displayed(job)
        documents = build_projection_documents(job, active, pod=self._pod, ip=self._ip)
        if not self._mongo_writer.upsert_and_verify(job, documents):
            raise RuntimeError("Mongo projection triple verification failed")


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
        failure_table_name: str = "jw_chat_agent_history_projection_enqueue_failure",
        default_user_id: int | None = None,
        max_attempts: int = 5,
        session_writer: SessionProjectionWriter | None = None,
    ) -> None:
        self._config = config
        self._table_name = qualified_table_name(table_name, setting="projection outbox table")
        self._failure_table_name = qualified_table_name(
            failure_table_name, setting="projection failure table"
        )
        self._default_user_id = default_user_id
        self._max_attempts = max_attempts
        self._session_writer = session_writer

    def record_enqueue_failure(
        self,
        *,
        source_log_id: int,
        session_id: str | None,
        source_kind: SourceKind,
        error_type: str,
        error_message: str,
    ) -> None:
        source_conversation_id = session_id or ""
        projected_session_id = (
            projection_session_id(source_conversation_id) if source_conversation_id else None
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._failure_table_name}
                        (source_log_id, projection_session_id, source_conversation_id,
                         source_kind, error_type, error_message, status, occurrences)
                    VALUES (%s, %s, %s, %s, %s, %s, 'enqueue_failed', 1)
                    ON DUPLICATE KEY UPDATE
                        error_type=VALUES(error_type), error_message=VALUES(error_message),
                        source_kind=VALUES(source_kind), occurrences=occurrences+1,
                        last_failed_at=NOW()
                    """,
                    (
                        source_log_id,
                        projected_session_id,
                        source_conversation_id or None,
                        source_kind,
                        error_type[:128],
                        f"{error_type}: projection enqueue failed"[:1000],
                    ),
                )
            connection.commit()

    def mark_enqueue_dead(self, *, source_log_id: int) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._failure_table_name}
                    SET status='dead', last_failed_at=NOW()
                    WHERE source_log_id=%s
                    """,
                    (source_log_id,),
                )
            connection.commit()
        LOGGER.error(
            "history_projection_enqueue_dead source_log_id=%s",
            source_log_id,
        )

    def source_conversation_id(self, session_id: str) -> str | None:
        if not session_id:
            return None
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute(
                    f"""
                    SELECT source_conversation_id
                    FROM {self._table_name}
                    WHERE session_id=%s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
        if not isinstance(row, Mapping):
            return None
        value = str(row.get("source_conversation_id") or "").strip()
        return value or None

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
        source_conversation_id = session_id
        projected_session_id = projection_session_id(source_conversation_id)
        source_kind = projection_context.source_kind if projection_context is not None else "unknown"
        trace_id = str(trace.get("trace_id") or uuid4())
        turn_id = trace_id
        span_id = uuid4().hex[:16]
        portal_user_id = (
            projection_context.portal_user_id
            if projection_context is not None and projection_context.portal_user_id is not None
            else self._default_user_id
        )
        headers = projection_context.http_headers if projection_context is not None else {}
        created_at = datetime.now(UTC)
        payload = {
            "question": question_text,
            "answer": answer_text,
            "charts": [dict(chart) for chart in charts],
            "sources": list(sources),
            "trace": dict(trace),
            "timing": dict(timing),
            "created_at": created_at.isoformat(),
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {self._table_name}
                        (source_log_id, session_id, source_conversation_id, source_kind,
                         turn_id, turn_index, projection_version,
                         trace_id, span_id, portal_user_id, request_headers_json, payload_json,
                         status, attempts, max_attempts, next_attempt_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            '{OUTBOX_PENDING_STATUS}', 0, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                        source_kind=VALUES(source_kind),
                        portal_user_id=VALUES(portal_user_id),
                        request_headers_json=VALUES(request_headers_json),
                        payload_json=VALUES(payload_json),
                        status='{OUTBOX_PENDING_STATUS}',
                        attempts=0,
                        next_attempt_at=NOW(),
                        last_error=NULL,
                        completed_at=NULL
                    """,
                    (
                        source_log_id,
                        projected_session_id,
                        source_conversation_id,
                        source_kind,
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
        if portal_user_id is not None and self._session_writer is not None:
            job = ProjectionJob(
                outbox_id=source_log_id,
                turn=CompletedTurn(
                    source_log_id=source_log_id,
                    session_id=projected_session_id,
                    turn_id=turn_id,
                    turn_index=turn_index,
                    question=question_text,
                    answer=answer_text,
                    charts=tuple(dict(chart) for chart in charts),
                    sources=tuple(sources),
                    trace=dict(trace),
                    timing=dict(timing),
                    created_at=created_at,
                    source_conversation_id=source_conversation_id,
                    source_kind=source_kind,
                ),
                projection_version=PROJECTION_VERSION,
                trace_id=trace_id,
                span_id=span_id,
                portal_user_id=portal_user_id,
                request_headers=dict(headers),
                attempts=0,
            )
            active = self._session_writer.active_service()
            self._session_writer.upsert_hidden(job, active)
            self._session_writer.mark_displayed(job)

    def claim_next(self) -> ProjectionJob | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT * FROM {self._table_name}
                    WHERE projection_version=%s AND (
                        (
                            status IN ('{OUTBOX_PENDING_STATUS}', '{OUTBOX_RETRY_STATUS}')
                            AND next_attempt_at <= NOW()
                        ) OR (
                            status = '{OUTBOX_PROCESSING_STATUS}'
                            AND updated_at < DATE_SUB(NOW(), INTERVAL 5 MINUTE)
                        )
                    )
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (PROJECTION_VERSION,),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                cursor.execute(
                    f"UPDATE {self._table_name} "
                    f"SET status='{OUTBOX_PROCESSING_STATUS}', updated_at=NOW() WHERE id=%s",
                    (row["id"],),
                )
            connection.commit()
        return _job_from_row(row)

    def complete(self, outbox_id: int) -> None:
        self._set_status(outbox_id, "completed", None, completed=True)

    def complete_current(self, job: ProjectionJob) -> None:
        if not job.payload_json:
            self.complete(job.outbox_id)
            return
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._table_name}
                    SET status='completed', last_error=NULL,
                        updated_at=NOW(), completed_at=NOW()
                    WHERE id=%s AND status='{OUTBOX_PROCESSING_STATUS}'
                      AND payload_json=%s
                    """,
                    (job.outbox_id, job.payload_json),
                )
            connection.commit()

    def fail(self, job: ProjectionJob, error: Exception) -> None:
        attempts = job.attempts + 1
        status = (
            "dead"
            if attempts >= self._max_attempts or isinstance(error, ProjectionOwnershipError)
            else OUTBOX_RETRY_STATUS
        )
        delay = min(300, 2 ** min(attempts, 8))
        message = f"{type(error).__name__}: {error}"[:1000]
        LOGGER.warning(
            "history_projection_retry outbox_id=%s trace_id=%s retry_attempt=%d "
            "max_attempts=%d status=%s dead_transition=%s error_type=%s",
            job.outbox_id,
            job.trace_id,
            attempts,
            self._max_attempts,
            status,
            str(status == "dead").lower(),
            type(error).__name__,
        )
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

    def requeue_dead_network_timeouts(self, *, since: datetime, until: datetime) -> int:
        if since >= until:
            raise ValueError("requeue time window must have since before until")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE {self._table_name}
                    SET status='{OUTBOX_RETRY_STATUS}', attempts=0,
                        next_attempt_at=NOW(), updated_at=NOW()
                    WHERE status='dead'
                      AND projection_version=%s
                      AND last_error LIKE 'NetworkTimeout:%%'
                      AND updated_at >= %s AND updated_at <= %s
                    """,
                    (PROJECTION_VERSION, since, until),
                )
                requeued = int(cursor.rowcount)
            connection.commit()
        return requeued

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
    def __init__(
        self,
        config: ProjectionDbConfig,
        *,
        endpoint: str,
        cache_ttl_seconds: int = 60,
        table_name: str = "llmops.chat_session_tb",
    ) -> None:
        self._config = config
        self._endpoint = endpoint
        self._cache_ttl_seconds = cache_ttl_seconds
        self._table_name = qualified_table_name(table_name, setting="projection session table")
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
                    f"SELECT id, reg_user_id FROM {self._table_name} WHERE uid=%s AND is_active=1 FOR UPDATE",
                    (job.turn.session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"""
                        INSERT INTO {self._table_name}
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
                        f"""
                        UPDATE {self._table_name}
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
                    f"""
                    UPDATE {self._table_name} SET is_display=1
                    WHERE uid=%s AND reg_user_id=%s AND is_active=1
                    """,
                    (job.turn.session_id, job.portal_user_id),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        f"""
                        SELECT reg_user_id, is_display FROM {self._table_name}
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

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        server_selection_timeout_ms: int = MONGO_SERVER_SELECTION_TIMEOUT_MS,
        connect_timeout_ms: int = MONGO_CONNECT_TIMEOUT_MS,
        socket_timeout_ms: int = MONGO_SOCKET_TIMEOUT_MS,
    ) -> None:
        from pymongo import MongoClient

        self._client = MongoClient(
            host=host,
            port=port,
            username=username,
            password=password,
            authSource=database,
            serverSelectionTimeoutMS=server_selection_timeout_ms,
            connectTimeoutMS=connect_timeout_ms,
            socketTimeoutMS=socket_timeout_ms,
            retryWrites=True,
        )
        self._database = self._client[database]

    def upsert_and_verify(self, job: ProjectionJob, documents: tuple[dict, dict, dict]) -> bool:
        acknowledgements = []
        for collection_name, document in zip(self._COLLECTIONS, documents, strict=True):
            update = (
                _response_projection_update(document)
                if collection_name == "chat-api_response"
                else {"$setOnInsert": document}
            )
            result = self._database[collection_name].update_one(
                {
                    "trace_id": job.trace_id,
                    "span_id": job.span_id,
                    "origin": PROJECTION_ORIGIN,
                    "history_projection_version": job.projection_version,
                },
                update,
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
                    complete_current = getattr(self._outbox, "complete_current", None)
                    if callable(complete_current):
                        complete_current(job)
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
        session_writer = MySQLSessionProjectionWriter(
            db_config,
            endpoint=endpoint,
            table_name=os.environ.get(
                "HISTORY_PROJECTION_SESSION_TABLE", "llmops.chat_session_tb"
            ).strip(),
        )
        outbox = MySQLProjectionOutbox(
            db_config,
            table_name=os.environ.get(
                "HISTORY_PROJECTION_OUTBOX_TABLE",
                "jw_chat_agent_history_projection_outbox",
            ).strip(),
            failure_table_name=os.environ.get(
                "HISTORY_PROJECTION_FAILURE_TABLE",
                "jw_chat_agent_history_projection_enqueue_failure",
            ).strip(),
            default_user_id=default_user_id,
            max_attempts=_positive_int_env("HISTORY_PROJECTION_MAX_ATTEMPTS", default=5),
            session_writer=session_writer,
        )
        mongo_writer = PyMongoProjectionWriter(
            host=mongo_values["HISTORY_PROJECTION_MONGO_HOST"],
            port=int(mongo_values["HISTORY_PROJECTION_MONGO_PORT"]),
            database=mongo_values["HISTORY_PROJECTION_MONGO_DATABASE"],
            username=mongo_values["HISTORY_PROJECTION_MONGO_USERNAME"],
            password=mongo_values["HISTORY_PROJECTION_MONGO_PASSWORD"],
            server_selection_timeout_ms=_positive_int_env(
                "HISTORY_PROJECTION_MONGO_SERVER_SELECTION_TIMEOUT_MS",
                default=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            ),
            connect_timeout_ms=_positive_int_env(
                "HISTORY_PROJECTION_MONGO_CONNECT_TIMEOUT_MS",
                default=MONGO_CONNECT_TIMEOUT_MS,
            ),
            socket_timeout_ms=_positive_int_env(
                "HISTORY_PROJECTION_MONGO_SOCKET_TIMEOUT_MS",
                default=MONGO_SOCKET_TIMEOUT_MS,
            ),
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
    progress_events = turn.trace.get("progress_events")
    if isinstance(progress_events, Sequence) and not isinstance(progress_events, str | bytes):
        for index, event in enumerate(progress_events, start=1):
            if not isinstance(event, Mapping):
                continue
            name = str(event.get("name") or "진행 단계").strip() or "진행 단계"
            detail = str(event.get("detail") or "").strip()
            node_id = f"direct-progress-{turn.turn_id}-{index}"
            flow.append(
                {
                    "nodeId": node_id,
                    "nodeLabel": name,
                    "data": {
                        "id": node_id,
                        "name": name,
                        "input": {"question": turn.question},
                        "output": {
                            "detail": detail,
                            "recorded_at": str(event.get("recorded_at") or ""),
                            "status": str(event.get("status") or "done"),
                        },
                        "state": {
                            "restored": True,
                            "schema": "r12.6.progress.v1",
                        },
                    },
                    "previousNodeIds": [previous],
                    "status": "FINISHED",
                }
            )
            previous = node_id
        return flow
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
    payload_json = str(row["payload_json"])
    payload = _json_loads(payload_json)
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
        source_conversation_id=str(row.get("source_conversation_id") or row["session_id"]),
        source_kind=source_kind_from_value(row.get("source_kind")),
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
        payload_json=payload_json,
    )


def _connect_mysql(config: ProjectionDbConfig, *, database: str | None = None, dict_cursor: bool = False):
    kwargs: dict[str, Any] = {}
    if dict_cursor:
        kwargs["cursorclass"] = pymysql.cursors.DictCursor
    io_timeout = _positive_int_env("HISTORY_PROJECTION_MYSQL_IO_TIMEOUT_SECONDS", default=120)
    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=database or config.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=3,
        read_timeout=io_timeout,
        write_timeout=io_timeout,
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


def _positive_int_env(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _json_dumps(value: object) -> str:
    # default= keeps the str() fallback the outbox has always had, but a mart point now lands as
    # the object the loader read instead of as a repr string that loses ms, rank and unknown keys.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=mart_json_default_or_str,
    )


def _json_loads(value: object) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}
