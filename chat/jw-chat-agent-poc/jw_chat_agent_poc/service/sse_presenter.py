from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Protocol
from uuid import UUID

from jw_chat_agent_poc.service.sse_protocol import iter_markdown_sse_events


SSE_PRESENTER_ENV: Final = "JW_CHAT_SSE_PRESENTER_ENABLED"


def sse_delta(token: str) -> str:
    lines = token.split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"event: delta\n{data}\n\n"


def sse_json_event(event_name: str, payload: object) -> str:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )
    lines = data.split("\n")
    encoded = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_name}\n{encoded}\n\n"


def legacy_sse_delta(token: str) -> str:
    lines = token.split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"event: delta\n{data}\n\n"


def legacy_sse_json_event(event_name: str, payload: object) -> str:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )
    lines = data.split("\n")
    encoded = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_name}\n{encoded}\n\n"


def _json_default(value: object) -> object:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def iter_busy_events(busy_message: str) -> Iterator[str]:
    yield sse_delta(busy_message)
    yield sse_json_event(
        "error",
        {"type": "ServiceBusy", "message": busy_message},
    )
    yield "event: done\ndata: error\n\n"


def iter_legacy_busy_events(busy_message: str) -> Iterator[str]:
    yield legacy_sse_delta(busy_message)
    yield legacy_sse_json_event(
        "error",
        {"type": "ServiceBusy", "message": busy_message},
    )
    yield "event: done\ndata: error\n\n"


def iter_initial_text_events(
    *,
    conversation_id: str | None,
    source_labels: Sequence[str],
    text: str,
) -> Iterator[str]:
    if conversation_id:
        yield f"event: conversation\ndata: {conversation_id}\n\n"
    yield f"event: sources\ndata: {','.join(source_labels)}\n\n"
    yield from iter_markdown_sse_events(text)


def iter_legacy_initial_text_events(
    *,
    conversation_id: str | None,
    source_labels: Sequence[str],
    text: str,
) -> Iterator[str]:
    if conversation_id:
        yield f"event: conversation\ndata: {conversation_id}\n\n"
    yield f"event: sources\ndata: {','.join(source_labels)}\n\n"
    yield from iter_markdown_sse_events(text)


def iter_final_answer_events(
    *,
    conversation_id: str | None,
    source_labels: Sequence[str],
    file_sources: Sequence[Mapping[str, Any]],
    text: str,
    charts: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]] = (),
    timing: Mapping[str, Any],
    trace: Mapping[str, Any],
    streamed_prefix: str = "",
) -> Iterator[str]:
    if conversation_id and not streamed_prefix:
        yield f"event: conversation\ndata: {conversation_id}\n\n"
    labels = list(source_labels)
    for item in file_sources:
        file_name = (
            str(item.get("file_name") or "").replace("\n", " ").replace(",", "，").strip()
        )
        label = f"업로드 문서: {file_name}" if file_name else ""
        if label and label not in labels:
            labels.append(label)
    if not streamed_prefix:
        yield f"event: sources\ndata: {','.join(labels)}\n\n"
    if file_sources and not streamed_prefix:
        yield sse_json_event("file_sources", list(file_sources))
    remaining_text = text
    if streamed_prefix and remaining_text.startswith(streamed_prefix):
        remaining_text = remaining_text[len(streamed_prefix) :].lstrip()
    if remaining_text:
        yield from iter_markdown_sse_events(remaining_text)
    if tables:
        yield sse_json_event("tables", tables)
    if charts:
        yield sse_json_event("charts", charts)
    yield sse_json_event("timing", timing)
    yield sse_json_event("trace", trace)
    yield "event: done\ndata: ok\n\n"


def iter_legacy_final_answer_events(
    *,
    conversation_id: str | None,
    source_labels: Sequence[str],
    file_sources: Sequence[Mapping[str, Any]],
    text: str,
    charts: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]] = (),
    timing: Mapping[str, Any],
    trace: Mapping[str, Any],
    streamed_prefix: str = "",
) -> Iterator[str]:
    if conversation_id and not streamed_prefix:
        yield f"event: conversation\ndata: {conversation_id}\n\n"
    labels = list(source_labels)
    for item in file_sources:
        file_name = (
            str(item.get("file_name") or "").replace("\n", " ").replace(",", "，").strip()
        )
        label = f"업로드 문서: {file_name}" if file_name else ""
        if label and label not in labels:
            labels.append(label)
    if not streamed_prefix:
        yield f"event: sources\ndata: {','.join(labels)}\n\n"
    if file_sources and not streamed_prefix:
        yield legacy_sse_json_event("file_sources", list(file_sources))
    remaining_text = text
    if streamed_prefix and remaining_text.startswith(streamed_prefix):
        remaining_text = remaining_text[len(streamed_prefix) :].lstrip()
    if remaining_text:
        yield from iter_markdown_sse_events(remaining_text)
    if tables:
        yield legacy_sse_json_event("tables", tables)
    if charts:
        yield legacy_sse_json_event("charts", charts)
    yield legacy_sse_json_event("timing", timing)
    yield legacy_sse_json_event("trace", trace)
    yield "event: done\ndata: ok\n\n"


class SsePresenter(Protocol):
    def delta(self, token: str) -> str: ...

    def json_event(self, event_name: str, payload: object) -> str: ...

    def busy_events(self, busy_message: str) -> Iterator[str]: ...

    def initial_text_events(
        self,
        *,
        conversation_id: str | None,
        source_labels: Sequence[str],
        text: str,
    ) -> Iterator[str]: ...

    def final_answer_events(
        self,
        *,
        conversation_id: str | None,
        source_labels: Sequence[str],
        file_sources: Sequence[Mapping[str, Any]],
        text: str,
        charts: Sequence[Mapping[str, Any]],
        tables: Sequence[Mapping[str, Any]] = (),
        timing: Mapping[str, Any],
        trace: Mapping[str, Any],
        streamed_prefix: str = "",
    ) -> Iterator[str]: ...


@dataclass(frozen=True, slots=True)
class ExtractedSsePresenter:
    delta = staticmethod(sse_delta)
    json_event = staticmethod(sse_json_event)
    busy_events = staticmethod(iter_busy_events)
    initial_text_events = staticmethod(iter_initial_text_events)
    final_answer_events = staticmethod(iter_final_answer_events)


@dataclass(frozen=True, slots=True)
class LegacySsePresenter:
    delta = staticmethod(legacy_sse_delta)
    json_event = staticmethod(legacy_sse_json_event)
    busy_events = staticmethod(iter_legacy_busy_events)
    initial_text_events = staticmethod(iter_legacy_initial_text_events)
    final_answer_events = staticmethod(iter_legacy_final_answer_events)


_EXTRACTED_PRESENTER: Final[SsePresenter] = ExtractedSsePresenter()
_LEGACY_PRESENTER: Final[SsePresenter] = LegacySsePresenter()


def selected_sse_presenter() -> SsePresenter:
    enabled = os.environ.get(SSE_PRESENTER_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    return _EXTRACTED_PRESENTER if enabled else _LEGACY_PRESENTER
