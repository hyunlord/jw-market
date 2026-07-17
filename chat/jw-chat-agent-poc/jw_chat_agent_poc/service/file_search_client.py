from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import requests

from jw_chat_agent_poc.service.file_sql_query import (
    SqlFileSource,
    fetch_sql_schema_columns,
    query_uploaded_sql,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadedFileSearchResult:
    file_context: str
    file_sources: tuple[str, ...]
    errors: tuple[str, ...]
    file_source_items: tuple[dict[str, Any], ...] = ()
    has_active_file: bool = True
    deterministic_answer: str = ""
    sql_trace: tuple[dict[str, str], ...] = ()


def search_uploaded_files(
    question: str,
    conversation_id: str | None,
    *,
    include_all_files: bool = False,
) -> UploadedFileSearchResult | None:
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
    supplemental_body: dict[str, Any] = {}
    if body.get("document_count") and _DOCUMENT_CONCLUSION_QUESTION_RE.search(question) is not None:
        supplemental_payload = {
            **payload,
            "question": _DOCUMENT_CONCLUSION_SUPPLEMENT_QUERY,
        }
        try:
            supplemental_response = requests.post(
                f"{base_url}/search",
                json=supplemental_payload,
                timeout=timeout_s,
            )
            supplemental_response.raise_for_status()
            supplemental_body = supplemental_response.json()
        except (requests.RequestException, ValueError):
            supplemental_body = {}
        context = _merge_search_contexts(
            context,
            str(supplemental_body.get("file_context") or ""),
        )
    has_active_file = bool(body.get("document_count"))
    if not context and not has_active_file:
        return None
    raw_sources = body.get("file_sources") or []
    supplemental_sources = supplemental_body.get("file_sources") or []
    if isinstance(raw_sources, list) and isinstance(supplemental_sources, list):
        raw_sources = [*raw_sources, *supplemental_sources]
    raw_sql_sources = body.get("sql_sources") or []
    requested_names = (
        frozenset()
        if include_all_files
        else _requested_file_names(question, raw_sources, raw_sql_sources)
    )
    if requested_names:
        context = _filter_file_context(context, requested_names)
        raw_sources = _filter_named_sources(raw_sources, requested_names)
        raw_sql_sources = _filter_named_sources(raw_sql_sources, requested_names)
    context = _prioritize_document_overview_context(question, context)
    sources = []
    items: list[dict[str, Any]] = []
    seen_items: set[tuple[str, str]] = set()
    if isinstance(raw_sources, list):
        for source in raw_sources:
            if isinstance(source, dict):
                name = str(source.get("file_name") or "업로드 문서")
                if name:
                    sources.append(name)
                item: dict[str, Any] = {"file_name": name}
                if source.get("document_id") is not None:
                    item["document_id"] = source["document_id"]
                for key in (
                    "i_page",
                    "slide_number",
                    "section_title",
                    "source_channel",
                    "sheet_name",
                    "row_start",
                    "row_end",
                ):
                    if source.get(key) is not None:
                        item[key] = source[key]
                key = (name, str(item.get("document_id", "")))
                if key not in seen_items:
                    seen_items.add(key)
                    items.append(item)
    errors = [
        str(error)
        for error in [*(body.get("errors") or []), *(supplemental_body.get("errors") or [])]
        if error
    ]
    deterministic_answer = ""
    sql_trace: tuple[dict[str, str], ...] = ()
    sql_sources = _sql_sources(raw_sql_sources)
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
        deterministic_answer = sql_outcome.answer_md
        sql_trace = sql_outcome.trace
    return UploadedFileSearchResult(
        file_context=context,
        file_sources=tuple(dict.fromkeys(sources)),
        errors=tuple(dict.fromkeys(errors)),
        file_source_items=tuple(items),
        has_active_file=True,
        deterministic_answer=deterministic_answer,
        sql_trace=sql_trace,
    )


def has_active_uploaded_file(conversation_id: str | None) -> bool:
    """Check session file ownership without retrieving file contents."""

    if not conversation_id or os.getenv("JW_CHAT_FILE_SEARCH_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return False
    base_url = os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")
    workflow_id = int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301"))
    timeout_s = float(os.getenv("JW_CHAT_FILE_PROBE_TIMEOUT_S", "3"))
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
        return False
    return bool(body.get("documents"))


def fetch_uploaded_file_schema_columns(conversation_id: str | None) -> tuple[str, ...]:
    """Read the active session's public SQL sources and their source columns."""

    if not conversation_id or os.getenv("JW_CHAT_FILE_SEARCH_ENABLED", "true").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return ()
    base_url = os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")
    workflow_id = int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301"))
    timeout_s = float(os.getenv("JW_CHAT_FILE_PROBE_TIMEOUT_S", "3"))
    try:
        response = requests.post(
            f"{base_url}/search",
            json={
                "workflow_id": workflow_id,
                "app_session_id": conversation_id,
                "chat_id": conversation_id,
                "question": "업로드 파일의 열 구조를 확인합니다.",
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("file search response must be an object")
        sources = _sql_sources(body.get("sql_sources"))
        if not sources:
            return ()
        return fetch_sql_schema_columns(conversation_id, sources)
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        logger.warning("file schema probe failed reason=%s", exc)
        return ()


def _sql_sources(raw_sources: Any) -> tuple[SqlFileSource, ...]:
    if not isinstance(raw_sources, list):
        return ()
    sources: list[SqlFileSource] = []
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            logger.warning(
                "discarding invalid file SQL source index=%d reason=source is not an object",
                index,
            )
            continue
        try:
            document_id = raw.get("document_id")
            sources.append(
                SqlFileSource(
                    logical_name=str(raw["logical_name"]),
                    file_name=str(raw["file_name"]),
                    sheet_name=str(raw["sheet_name"]),
                    document_id=int(document_id) if document_id is not None else None,
                    row_count=int(raw["row_count"]) if raw.get("row_count") is not None else None,
                    column_count=int(raw["column_count"]) if raw.get("column_count") is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "discarding invalid file SQL source index=%d reason=%s",
                index,
                exc,
            )
            continue
    return tuple(sources)


def _join_contexts(*values: str) -> str:
    return "\n\n".join(value.strip() for value in values if value.strip())


def _merge_search_contexts(*values: str) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for value in values:
        for block in re.split(r"\n\n(?=\[\d+\]\s)", value.strip()):
            normalized = block.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                blocks.append(normalized)
    return "\n\n".join(blocks)


def _requested_file_names(question: str, *source_groups: Any) -> frozenset[str]:
    lowered = question.casefold()
    names = {
        match.casefold()
        for match in re.findall(
            r"[^\s/\\]+\.(?:xlsx?|xlsm|csv|pdf|docx?|pptx?)",
            question,
            re.IGNORECASE,
        )
    }
    for group in source_groups:
        if not isinstance(group, list):
            continue
        for source in group:
            if not isinstance(source, dict):
                continue
            name = str(source.get("file_name") or "").strip()
            stem = name.rsplit(".", 1)[0].casefold() if "." in name else ""
            stem_pattern = (
                rf"(?<![0-9a-z가-힣]){re.escape(stem)}"
                r"(?=$|[\s.,?!()\[\]{}]|에서|의|은|는|이|가|을|를|와|과)"
            )
            if name and (
                name.casefold() in lowered
                or (len(stem) >= 2 and re.search(stem_pattern, lowered))
            ):
                names.add(name.casefold())
    return frozenset(names)


def _filter_named_sources(raw_sources: Any, requested_names: frozenset[str]) -> list[Any]:
    if not isinstance(raw_sources, list):
        return []
    return [
        source
        for source in raw_sources
        if isinstance(source, dict)
        and str(source.get("file_name") or "").strip().casefold() in requested_names
    ]


def _filter_file_context(context: str, requested_names: frozenset[str]) -> str:
    if not context:
        return ""
    blocks = re.split(r"\n\n(?=\[\d+\]\s)", context)
    selected = [
        block
        for block in blocks
        if any(name in block.casefold() for name in requested_names)
    ]
    return "\n\n".join(selected)


_DOCUMENT_OVERVIEW_QUESTION_RE = re.compile(
    r"(?:문서|보고서|파일|발표).{0,16}(?:요약|핵심|결론|뭐에\s*관한|무슨\s*내용)"
    r"|(?:요약|핵심|결론).{0,16}(?:문서|보고서|파일|발표)",
    re.IGNORECASE,
)
_DOCUMENT_OVERVIEW_HEADING_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?"
    r"(?:key\s+takeaways?|executive\s+summary|conclusions?|summary|"
    r"unmet\s+needs?|핵심\s*요약|주요\s*요약|결론|요약)\b",
    re.IGNORECASE,
)
_DOCUMENT_CONCLUSION_QUESTION_RE = re.compile(r"결론|시사점|요점", re.IGNORECASE)
_DOCUMENT_CONCLUSION_SIGNAL_RE = re.compile(
    r"(?:conclusions?|unmet\s+needs?|recommendations?|implications?|결론|시사점|미충족\s*수요|권고)",
    re.IGNORECASE,
)
_DOCUMENT_CONCLUSION_SUPPLEMENT_QUERY = (
    "문서 전체 결론 핵심 시사점 권고 미충족 수요 "
    "conclusion key takeaways unmet needs recommendation"
)


def _document_overview_rank(question: str, indexed_block: tuple[int, str]) -> tuple[int, int, int, int]:
    original_index, block = indexed_block
    heading_match = _DOCUMENT_OVERVIEW_HEADING_RE.search(block[:600])
    is_overview = heading_match is not None
    wants_conclusion = _DOCUMENT_CONCLUSION_QUESTION_RE.search(question) is not None
    has_conclusion_signal = _DOCUMENT_CONCLUSION_SIGNAL_RE.search(block) is not None
    body = block[heading_match.end() :] if heading_match is not None else block
    substance = len(re.findall(r"[0-9A-Za-z가-힣]+", body))
    return (
        0 if is_overview else 1,
        0 if wants_conclusion and has_conclusion_signal else 1,
        -substance if is_overview else 0,
        original_index,
    )


def _prioritize_document_overview_context(question: str, context: str) -> str:
    """Move explicit overview sections first without dropping retrieved evidence."""

    if not context or _DOCUMENT_OVERVIEW_QUESTION_RE.search(question) is None:
        return context
    blocks = re.split(r"\n\n(?=\[\d+\]\s)", context)
    if len(blocks) < 2:
        return context
    ranked = sorted(
        enumerate(blocks),
        key=lambda item: _document_overview_rank(question, item),
    )
    return "\n\n".join(block for _, block in ranked)


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
