from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from jw_chat_agent_poc.service.file_sql_query import (
    SqlFileSource,
    query_uploaded_sql,
)


@dataclass(frozen=True, slots=True)
class UploadedFileSearchResult:
    file_context: str
    file_sources: tuple[str, ...]
    errors: tuple[str, ...]
    file_source_items: tuple[dict[str, Any], ...] = ()
    has_active_file: bool = True


def search_uploaded_files(question: str, conversation_id: str | None) -> UploadedFileSearchResult | None:
    """Query wf301 file bridge by conversation/session id.

    The bridge owns file-session isolation. Chat only forwards the stable
    conversation id as both chat_id and app_session_id, then consumes the
    unified file_context it returns (wiki-first inside 235, VDB fallback there).
    """
    if not conversation_id or os.getenv("JW_CHAT_FILE_SEARCH_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return None
    base_url = os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")
    workflow_id = int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301"))
    timeout_s = float(os.getenv("JW_CHAT_FILE_SEARCH_TIMEOUT_S", "3"))
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "app_session_id": conversation_id,
        "chat_id": conversation_id,
        "question": question,
    }
    try:
        response = requests.post(f"{base_url}/search", json=payload, timeout=timeout_s)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return _active_file_fallback(
            base_url=base_url,
            workflow_id=workflow_id,
            conversation_id=conversation_id,
            timeout_s=timeout_s,
        )
    context = str(body.get("file_context") or "").strip()
    has_active_file = bool(body.get("document_count"))
    if not context and not has_active_file:
        return None
    raw_sources = body.get("file_sources") or []
    sources = []
    items: list[dict[str, Any]] = []
    seen_items: set[tuple[str, str]] = set()
    if isinstance(raw_sources, list):
        for source in raw_sources:
            if isinstance(source, dict):
                name = str(source.get("file_name") or source.get("chunk_id") or "uploaded file")
                if name:
                    sources.append(name)
                item: dict[str, Any] = {"file_name": name}
                if source.get("document_id") is not None:
                    item["document_id"] = source["document_id"]
                key = (name, str(item.get("document_id", "")))
                if key not in seen_items:
                    seen_items.add(key)
                    items.append(item)
    errors = [str(error) for error in (body.get("errors") or []) if error]
    sql_sources = _sql_sources(body.get("sql_sources"))
    if body.get("sql_available") and sql_sources:
        sql_outcome = query_uploaded_sql(question, conversation_id, sql_sources)
        context = _join_contexts(context, sql_outcome.file_context)
        for item in sql_outcome.file_source_items:
            name = str(item.get("file_name") or "uploaded file")
            key = (name, str(item.get("document_id", "")))
            if key not in seen_items:
                seen_items.add(key)
                items.append(dict(item))
            if name:
                sources.append(name)
        errors.extend(sql_outcome.errors)
    return UploadedFileSearchResult(
        file_context=context,
        file_sources=tuple(dict.fromkeys(sources)),
        errors=tuple(dict.fromkeys(errors)),
        file_source_items=tuple(items),
        has_active_file=True,
    )


def _sql_sources(raw_sources: Any) -> tuple[SqlFileSource, ...]:
    if not isinstance(raw_sources, list):
        return ()
    sources: list[SqlFileSource] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        try:
            sources.append(
                SqlFileSource(
                    logical_name=str(raw["logical_name"]),
                    file_name=str(raw["file_name"]),
                    sheet_name=str(raw["sheet_name"]),
                    document_id=int(raw["document_id"]),
                    row_count=int(raw["row_count"]) if raw.get("row_count") is not None else None,
                    column_count=int(raw["column_count"]) if raw.get("column_count") is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(sources)


def _join_contexts(*values: str) -> str:
    return "\n\n".join(value.strip() for value in values if value.strip())


def _active_file_fallback(
    *,
    base_url: str,
    workflow_id: int,
    conversation_id: str,
    timeout_s: float,
) -> UploadedFileSearchResult | None:
    try:
        response = requests.get(
            f"{base_url}/documents",
            params={
                "workflow_id": workflow_id,
                "app_session_id": conversation_id,
                "chat_id": conversation_id,
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None
    if not body.get("documents"):
        return None
    return UploadedFileSearchResult(
        file_context="",
        file_sources=(),
        errors=("file search unavailable",),
        has_active_file=True,
    )
