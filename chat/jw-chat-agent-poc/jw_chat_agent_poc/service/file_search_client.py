from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


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
    errors = body.get("errors") or []
    return UploadedFileSearchResult(
        file_context=context,
        file_sources=tuple(dict.fromkeys(sources)),
        errors=tuple(str(error) for error in errors if error),
        file_source_items=tuple(items),
        has_active_file=True,
    )


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
