from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import requests

from jw_chat_agent_poc.service.actor_context import code_serving_actor_headers
from jw_chat_agent_poc.service.file_sql_query import (
    SqlFileSource,
    fetch_sql_schema_columns,
    query_uploaded_sql,
)

logger = logging.getLogger(__name__)


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True, slots=True)
class UploadedFileSearchResult:
    file_context: str
    file_sources: tuple[str, ...]
    errors: tuple[str, ...]
    file_source_items: tuple[dict[str, Any], ...] = ()
    has_active_file: bool = True
    deterministic_answer: str = ""
    sql_trace: tuple[dict[str, str], ...] = ()
    rag_query: str = ""
    rag_queries: tuple[str, ...] = ()
    rag_top_k: int | None = None
    sql_detail: dict[str, Any] = field(default_factory=dict)
    detail_source_items: tuple[dict[str, Any], ...] = ()
    record_context: str = ""
    rag_top_k_source: str = ""
    prompt_context_limit: int | None = None
    rag_trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _DocumentInventoryProbe:
    documents: tuple[dict[str, Any], ...]
    failure_class: str | None = None
    failure_reason: str | None = None
    attempts: int = 0
    elapsed_ms: float = 0.0
    timeout_s: float = 0.0


class _FileInventoryUnavailable(RuntimeError):
    def __init__(self, probe: _DocumentInventoryProbe) -> None:
        super().__init__(probe.failure_reason or "file inventory unavailable")
        self.probe = probe


@dataclass(frozen=True, slots=True)
class UploadedSqlTableOverview:
    sheet_name: str
    row_count: int
    column_count: int


@dataclass(frozen=True, slots=True)
class UploadedWorksheetOverview:
    name: str
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True, slots=True)
class UploadedFileOverview:
    file_name: str
    storage_route: str
    chunk_count: int
    sql_tables: tuple[UploadedSqlTableOverview, ...] = ()
    title: str | None = None
    sheet_count: int | None = None
    sheets: tuple[UploadedWorksheetOverview, ...] = ()
    page_count: int | None = None
    slide_count: int | None = None


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
    if os.getenv("JW_CHAT_FILE_SEARCH_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return None
    if not conversation_id:
        return _no_document_file_result(question) if _should_prefetch_file_routes(question) else None
    base_url = os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")
    workflow_id = int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301"))
    # Measured against the live bridge on 2026-08-20: a cold /search over a
    # freshly committed 37-chunk PDF answered in 4.57s, 5.42s and 5.88s across
    # three runs, and 0.85s once warm. The old 3s default therefore expired on
    # every cold retrieval, dropped into _active_file_fallback, and returned
    # has_active_file=True with an empty context - the "첨부 문서 근거를 가져오지
    # 못했습니다" answer, from a session whose file was fully indexed.
    #
    # 12s clears the observed cold range with headroom while staying far inside
    # the request budget, and it is only ever spent when the session has a file.
    timeout_s = float(os.getenv("JW_CHAT_FILE_SEARCH_TIMEOUT_S", "12"))
    search_question = _document_retrieval_question(question)
    sent_queries = [search_question]
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "app_session_id": conversation_id,
        "chat_id": conversation_id,
        "question": search_question,
    }
    requested_limit = _file_search_limit()
    payload["limit"] = requested_limit
    prefetched_documents: list[dict[str, Any]] = []
    inventory_probe = _DocumentInventoryProbe(())
    prefetched_sql_sources: tuple[SqlFileSource, ...] = ()
    sql_executor: ThreadPoolExecutor | None = None
    sql_future: Future[Any] | None = None
    # Uploads are a normal evidence lane. Inventory both file tools for every
    # turn so their execution does not depend on directive words in the query.
    if conversation_id:
        try:
            prefetched_documents = _fetch_session_documents(
                base_url=base_url,
                workflow_id=workflow_id,
                conversation_id=conversation_id,
                timeout_s=min(timeout_s, float(os.getenv("JW_CHAT_FILE_PROBE_TIMEOUT_S", "6"))),
            )
            inventory_probe = _DocumentInventoryProbe(tuple(prefetched_documents))
        except _FileInventoryUnavailable as exc:
            inventory_probe = exc.probe
        inventoried_documents = prefetched_documents
        prefetched_documents = [
            document for document in prefetched_documents if not _document_is_expired(document)
        ]
        if inventoried_documents and not prefetched_documents:
            return _expired_document_file_result(question, inventoried_documents)
        prefetched_raw_sql_sources = _session_document_sql_sources(prefetched_documents)
        requested_names = (
            frozenset()
            if include_all_files
            else _requested_file_names(question, prefetched_documents, prefetched_raw_sql_sources)
        )
        if requested_names:
            prefetched_raw_sql_sources = _filter_named_sources(
                prefetched_raw_sql_sources,
                requested_names,
            )
        prefetched_sql_sources = _sql_sources(prefetched_raw_sql_sources)
        if prefetched_sql_sources:
            sql_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="file-sql")
            request_context = copy_context()
            sql_future = sql_executor.submit(
                request_context.run,
                query_uploaded_sql,
                question,
                conversation_id,
                prefetched_sql_sources,
            )
    try:
        response = requests.post(
            f"{base_url}/search",
            json=payload,
            headers=code_serving_actor_headers(),
            timeout=timeout_s,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        if sql_future is not None:
            try:
                sql_outcome = sql_future.result(timeout=min(timeout_s, 2.0))
            except FutureTimeoutError:
                logger.warning("file SQL fallback did not finish within the search-failure budget")
                sql_future.cancel()
            # Worker failures must not hide the document-search fallback.
            except Exception:
                logger.exception("file SQL fallback failed after document search failure")
            else:
                route = _file_route_preference(question, prefetched_documents)
                if route != "document" or sql_outcome.status == "ok":
                    sql_context, sql_answer = _select_file_route_output(
                        route,
                        "",
                        sql_outcome.file_context,
                        sql_outcome.answer_md,
                    )
                    if sql_context or sql_answer:
                        source_items = tuple(dict(item) for item in sql_outcome.file_source_items)
                        return UploadedFileSearchResult(
                            file_context=sql_context,
                            file_sources=tuple(
                                dict.fromkeys(
                                    str(item.get("file_name") or "").strip()
                                    for item in source_items
                                    if str(item.get("file_name") or "").strip()
                                )
                            ),
                            errors=tuple(sql_outcome.errors),
                            file_source_items=source_items,
                            has_active_file=True,
                            deterministic_answer=sql_answer,
                            sql_trace=sql_outcome.trace,
                            sql_detail=dict(sql_outcome.detail),
                        )
            finally:
                if sql_executor is not None:
                    sql_executor.shutdown(wait=False, cancel_futures=True)
        elif sql_executor is not None:
            sql_executor.shutdown(wait=False, cancel_futures=True)
        if inventory_probe.failure_class is not None:
            return _unavailable_file_result(question, inventory_probe)
        fallback = _active_file_fallback(
            base_url=base_url,
            workflow_id=workflow_id,
            conversation_id=conversation_id,
            timeout_s=timeout_s,
            question=question,
        )
        if fallback is not None:
            return fallback
        return _no_document_file_result(question) if _should_prefetch_file_routes(question) else None
    context = str(body.get("file_context") or "").strip()
    initial_context = context
    supplemental_body: dict[str, Any] = {}
    rag_trace: dict[str, Any] = {
        "retry_count": 0,
        "retry_success": False,
        "retry_reason": "not_needed",
        "adjacent_window": 1,
        "same_page_boost": True,
        "table_intent_boost": _has_table_intent(question),
    }
    if body.get("document_count") and _DOCUMENT_OVERVIEW_QUESTION_RE.search(question) is not None:
        supplemental_payload = {
            **payload,
            "question": _DOCUMENT_CONCLUSION_SUPPLEMENT_QUERY,
        }
        try:
            sent_queries.append(_DOCUMENT_CONCLUSION_SUPPLEMENT_QUERY)
            supplemental_response = requests.post(
                f"{base_url}/search",
                json=supplemental_payload,
                headers=code_serving_actor_headers(),
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
    elif body.get("document_count") and _needs_document_retry(question, context):
        initial_sources = body.get("file_sources") or []
        supplemental_query = _document_supplement_query(
            question,
            context=context,
            raw_sources=initial_sources,
        )
        supplemental_payload = {**payload, "question": supplemental_query}
        rag_trace.update(
            {
                "retry_count": 1,
                "retry_reason": "core_token_missing",
                "supplement_strategy": "section_neighbor_query",
            }
        )
        try:
            sent_queries.append(supplemental_query)
            supplemental_response = requests.post(
                f"{base_url}/search",
                json=supplemental_payload,
                headers=code_serving_actor_headers(),
                timeout=timeout_s,
            )
            supplemental_response.raise_for_status()
            supplemental_body = supplemental_response.json()
        except (requests.RequestException, ValueError):
            supplemental_body = {}
        supplemental_context = str(supplemental_body.get("file_context") or "")
        supplemental_sources = supplemental_body.get("file_sources") or []
        rag_trace.update(
            _document_neighbor_trace(
                initial_sources,
                supplemental_sources,
                adjacent_window=1,
            )
        )
        rag_trace["retry_success"] = not _needs_document_retry(
            question,
            supplemental_context,
        )
        context = _merge_search_contexts(context, supplemental_context)
    has_active_file = bool(body.get("document_count")) or bool(prefetched_documents)
    if not context and not has_active_file and inventory_probe.failure_class is not None:
        return _unavailable_file_result(question, inventory_probe)
    if not context and not has_active_file:
        return _no_document_file_result(question) if _should_prefetch_file_routes(question) else None
    raw_sources = body.get("file_sources") or []
    supplemental_sources = supplemental_body.get("file_sources") or []
    context, raw_sources, merge_trace = _merge_search_materials(
        (initial_context, raw_sources),
        (str(supplemental_body.get("file_context") or ""), supplemental_sources),
    )
    rag_trace.update(merge_trace)
    context, raw_sources, rank_trace = _rerank_document_material(
        question,
        context,
        raw_sources,
        adjacent_window=1,
    )
    rag_trace.update(rank_trace)
    if prefetched_documents:
        rag_names = frozenset(
            str(document.get("file_name") or "").strip().casefold()
            for document in prefetched_documents
            if str(document.get("file_name") or "").strip()
        )
        context = _filter_file_context(context, rag_names) if rag_names else ""
        raw_sources = _filter_named_sources(raw_sources, rag_names) if rag_names else []
    raw_sql_sources = body.get("sql_sources") or []
    if _sql_sources_missing_document_id(raw_sql_sources):
        raw_sql_sources = _hydrate_sql_source_document_ids(
            raw_sql_sources,
            prefetched_documents or _fetch_session_documents_or_empty(
                base_url=base_url,
                workflow_id=workflow_id,
                conversation_id=conversation_id,
                timeout_s=timeout_s,
            ),
        )
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
    detail_items: list[dict[str, Any]] = []
    seen_items: set[tuple[str, ...]] = set()
    seen_detail_items: set[tuple[str, ...]] = set()
    inventory_document_ids = {
        str(document.get("file_name") or "").strip().casefold(): document.get("document_id")
        for document in prefetched_documents
        if str(document.get("file_name") or "").strip()
    }
    if isinstance(raw_sources, list):
        for source in raw_sources:
            if isinstance(source, dict):
                name = str(source.get("file_name") or "업로드 문서")
                if name:
                    sources.append(name)
                item: dict[str, Any] = {
                    "file_name": name,
                    "planned_lane": "document_rag",
                    "retrieval_route": "파일 검색",
                }
                if source.get("document_id") is not None:
                    item["document_id"] = source["document_id"]
                elif inventory_document_ids.get(name.casefold()) is not None:
                    item["document_id"] = inventory_document_ids[name.casefold()]
                for key in (
                    "i_page",
                    "page",
                    "slide_number",
                    "section_title",
                    "source_channel",
                    "sheet_name",
                    "row_start",
                    "row_end",
                    "chunk_id",
                    "i_chunk_on_doc",
                    "score",
                    "score_kind",
                    "similarity_score",
                    "distance",
                ):
                    if source.get(key) is not None:
                        item[key] = source[key]
                detail_key = tuple(
                    str(item.get(field, ""))
                    for field in (
                        "file_name", "document_id", "chunk_id", "i_page", "page",
                        "slide_number", "sheet_name", "row_start", "row_end",
                    )
                )
                if detail_key not in seen_detail_items:
                    seen_detail_items.add(detail_key)
                    detail_items.append(dict(item))
                public_item = {
                    key: value
                    for key, value in item.items()
                    if key not in {
                        "chunk_id", "i_chunk_on_doc", "score", "score_kind",
                        "similarity_score", "distance",
                    }
                }
                key = tuple(
                    str(public_item.get(field, ""))
                    for field in (
                        "file_name", "document_id", "i_page", "slide_number",
                        "sheet_name", "row_start", "row_end",
                    )
                )
                if key not in seen_items:
                    seen_items.add(key)
                    items.append(public_item)
    errors = [
        str(error)
        for error in [*(body.get("errors") or []), *(supplemental_body.get("errors") or [])]
        if error
    ]
    deterministic_answer = ""
    sql_trace: tuple[dict[str, str], ...] = ()
    sql_sources = _sql_sources(raw_sql_sources)
    sql_outcome = None
    if sql_future is not None:
        try:
            sql_outcome = sql_future.result()
        finally:
            if sql_executor is not None:
                sql_executor.shutdown(wait=True)
    elif body.get("sql_available") and sql_sources:
        sql_outcome = query_uploaded_sql(question, conversation_id, sql_sources)
    if sql_outcome is not None:
        rag_context = context
        for item in sql_outcome.file_source_items:
            name = str(item.get("file_name") or "uploaded file")
            key = tuple(
                str(item.get(field, ""))
                for field in (
                    "file_name", "document_id", "sheet_name", "row_start",
                    "row_end",
                )
            )
            if key not in seen_items:
                seen_items.add(key)
                items.append(dict(item))
                detail_items.append(dict(item))
            if name:
                sources.append(name)
        errors.extend(sql_outcome.errors)
        route = "both"
        context, deterministic_answer = _select_file_route_output(
            route,
            rag_context,
            sql_outcome.file_context,
            sql_outcome.answer_md,
        )
        sql_trace = sql_outcome.trace
    inventoried_rag_names = {
        str(item.get("file_name") or "").strip()
        for item in items
        if item.get("planned_lane") == "document_rag"
        or item.get("retrieval_route") == "파일 검색"
    }
    for document in prefetched_documents:
        name = str(document.get("file_name") or "").strip()
        if not name or name in inventoried_rag_names:
            continue
        inventory_item = {
            "file_name": name,
            "document_id": document.get("document_id"),
            "metadata_only": True,
            "planned_lane": "document_rag",
            "retrieval_route": "파일 검색",
        }
        items.append(inventory_item)
        detail_items.append(dict(inventory_item))
        sources.append(name)
    record_context = context
    prompt_context_limit = _file_prompt_context_limit()
    context = _bounded_file_prompt_context(record_context, prompt_context_limit)
    response_top_k = _optional_nonnegative_int(body.get("top_k"))
    return UploadedFileSearchResult(
        file_context=context,
        file_sources=tuple(dict.fromkeys(sources)),
        errors=tuple(dict.fromkeys(errors)),
        file_source_items=tuple(items),
        has_active_file=True,
        deterministic_answer=deterministic_answer,
        sql_trace=sql_trace,
        rag_query=search_question,
        rag_queries=tuple(sent_queries),
        rag_top_k=response_top_k if response_top_k is not None else requested_limit,
        sql_detail=(dict(sql_outcome.detail) if sql_outcome is not None else {}),
        detail_source_items=tuple(detail_items),
        record_context=record_context,
        rag_top_k_source="response" if response_top_k is not None else "request",
        prompt_context_limit=prompt_context_limit,
        rag_trace=rag_trace,
    )


_PARALLEL_FILE_ROUTE_RE = re.compile(
    r"(?:파일|문서|팩트\s*시트|리포트|보고서|가이드라인|pdf|엑셀|xlsx|xlsm|csv|시트|셀)",
    re.IGNORECASE,
)
_DOCUMENT_TARGET_RE = re.compile(
    r"(?:팩트\s*시트|리포트|보고서|가이드라인|pdf)",
    re.IGNORECASE,
)
_SPREADSHEET_TARGET_RE = re.compile(
    r"(?:엑셀|xlsx|xlsm|csv|어느\s*시트|어느\s*셀|시트명|sell[ -]?out|총액)",
    re.IGNORECASE,
)
_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt")
_SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")


def _should_prefetch_file_routes(question: str) -> bool:
    return (
        _PARALLEL_FILE_ROUTE_RE.search(question) is not None
        or _TERSE_DOCUMENT_OVERVIEW_RE.fullmatch(" ".join(question.split())) is not None
    )


def _session_document_sql_sources(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for document in documents:
        file_name = str(document.get("file_name") or "").strip()
        document_id = document.get("document_id")
        tables = document.get("sql_tables")
        if not file_name or not isinstance(tables, list):
            continue
        for table in tables:
            if not isinstance(table, dict):
                continue
            sources.append(
                {
                    **table,
                    "file_name": file_name,
                    "document_id": document_id,
                }
            )
    return sources


def _file_route_preference(question: str, *source_groups: Any) -> str:
    names = tuple(_source_file_names(*source_groups))
    has_document = any(name.lower().endswith(_DOCUMENT_SUFFIXES) for name in names)
    has_spreadsheet = any(name.lower().endswith(_SPREADSHEET_SUFFIXES) for name in names)
    document_target = _DOCUMENT_TARGET_RE.search(question) is not None
    spreadsheet_target = _SPREADSHEET_TARGET_RE.search(question) is not None
    if document_target and not spreadsheet_target and has_document:
        return "document"
    if spreadsheet_target and not document_target and has_spreadsheet:
        return "spreadsheet"
    return "both"


def _source_file_names(*source_groups: Any) -> tuple[str, ...]:
    names: list[str] = []
    for group in source_groups:
        if not isinstance(group, list):
            continue
        for source in group:
            if not isinstance(source, dict):
                continue
            name = str(source.get("file_name") or "").strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _select_file_route_output(
    route: str,
    rag_context: str,
    sql_context: str,
    sql_answer: str,
) -> tuple[str, str]:
    rag_context = rag_context.strip()
    sql_context = sql_context.strip()
    sql_answer = sql_answer.strip()
    if route == "document" and rag_context:
        return rag_context, ""
    if route == "spreadsheet" and (sql_answer or sql_context):
        return sql_context, sql_answer
    if rag_context and (sql_context or sql_answer):
        combined = _join_contexts(
            "## 업로드 문서 검색 결과\n" + rag_context,
            "## 업로드 스프레드시트 조회 결과\n" + (sql_context or sql_answer),
        )
        return combined, ""
    if rag_context:
        return rag_context, ""
    return sql_context, sql_answer


def has_active_uploaded_file(conversation_id: str | None) -> bool:
    """Check session file ownership without retrieving file contents."""

    if not conversation_id or os.getenv("JW_CHAT_FILE_SEARCH_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return False
    base_url = os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")
    workflow_id = int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301"))
    timeout_s = float(os.getenv("JW_CHAT_FILE_PROBE_TIMEOUT_S", "6"))
    try:
        return any(
            not _document_is_expired(document)
            for document in _fetch_session_documents(
                base_url=base_url,
                workflow_id=workflow_id,
                conversation_id=conversation_id,
                timeout_s=timeout_s,
            )
        )
    except _FileInventoryUnavailable:
        return False


def fetch_uploaded_file_overviews(
    conversation_id: str | None,
) -> tuple[UploadedFileOverview, ...]:
    """Return schema-only upload metadata without invoking search or an LLM."""

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
        response = requests.get(
            f"{base_url}/documents",
            params={
                "workflow_id": workflow_id,
                "app_session_id": conversation_id,
                "chat_id": conversation_id,
            },
            headers=code_serving_actor_headers(),
            timeout=timeout_s,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return ()
    documents = body.get("documents") if isinstance(body, dict) else None
    if not isinstance(documents, list):
        return ()
    overviews: list[UploadedFileOverview] = []
    for raw_document in documents:
        if not isinstance(raw_document, dict):
            continue
        if _document_is_expired(raw_document):
            continue
        file_name = str(raw_document.get("file_name") or "").strip()
        if not file_name:
            continue
        tables: list[UploadedSqlTableOverview] = []
        raw_tables = raw_document.get("sql_tables")
        if isinstance(raw_tables, list):
            for raw_table in raw_tables:
                if not isinstance(raw_table, dict):
                    continue
                try:
                    tables.append(
                        UploadedSqlTableOverview(
                            sheet_name=str(raw_table["sheet_name"]),
                            row_count=max(0, int(raw_table["row_count"])),
                            column_count=max(0, int(raw_table["column_count"])),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        try:
            chunk_count = max(0, int(raw_document.get("chunk_count") or 0))
        except (TypeError, ValueError):
            chunk_count = 0
        raw_card = raw_document.get("file_card")
        card = raw_card if isinstance(raw_card, dict) else {}
        worksheets: list[UploadedWorksheetOverview] = []
        raw_sheets = card.get("sheets")
        if isinstance(raw_sheets, list):
            for raw_sheet in raw_sheets:
                if not isinstance(raw_sheet, dict) or not str(raw_sheet.get("name") or "").strip():
                    continue
                worksheets.append(
                    UploadedWorksheetOverview(
                        name=str(raw_sheet["name"]),
                        row_count=_optional_nonnegative_int(raw_sheet.get("row_count")),
                        column_count=_optional_nonnegative_int(raw_sheet.get("column_count")),
                    )
                )
        overviews.append(
            UploadedFileOverview(
                file_name=file_name,
                storage_route=str(raw_document.get("storage_route") or "vdb"),
                chunk_count=chunk_count,
                sql_tables=tuple(tables),
                title=_optional_text(card.get("title")),
                sheet_count=_optional_nonnegative_int(card.get("sheet_count")),
                sheets=tuple(worksheets),
                page_count=_optional_nonnegative_int(card.get("page_count")),
                slide_count=_optional_nonnegative_int(card.get("slide_count")),
            )
        )
    return tuple(overviews)


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
                "limit": _file_search_limit(),
            },
            headers=code_serving_actor_headers(),
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


def _sql_sources_missing_document_id(raw_sources: Any) -> bool:
    return isinstance(raw_sources, list) and any(
        isinstance(source, dict) and source.get("document_id") is None
        for source in raw_sources
    )


def _fetch_session_documents(
    *,
    base_url: str,
    workflow_id: int,
    conversation_id: str,
    timeout_s: float,
) -> list[dict[str, Any]]:
    probe = _probe_session_documents(
        base_url=base_url,
        workflow_id=workflow_id,
        conversation_id=conversation_id,
        timeout_s=timeout_s,
    )
    if probe.failure_class is not None:
        raise _FileInventoryUnavailable(probe)
    return list(probe.documents)


def _fetch_session_documents_or_empty(**kwargs: Any) -> list[dict[str, Any]]:
    try:
        return _fetch_session_documents(**kwargs)
    except _FileInventoryUnavailable:
        return []


def _probe_session_documents(
    *,
    base_url: str,
    workflow_id: int,
    conversation_id: str,
    timeout_s: float,
) -> _DocumentInventoryProbe:
    started = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(
                f"{base_url}/documents",
                params={
                    "workflow_id": workflow_id,
                    "app_session_id": conversation_id,
                    "chat_id": conversation_id,
                },
                headers=code_serving_actor_headers(),
                timeout=timeout_s,
            )
            response.raise_for_status()
            body = response.json()
            documents = body.get("documents") if isinstance(body, dict) else None
            if not isinstance(documents, list):
                raise TypeError("file inventory response must contain a documents list")
            return _DocumentInventoryProbe(
                tuple(document for document in documents if isinstance(document, dict)),
                attempts=attempt + 1,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                timeout_s=timeout_s,
            )
        except (requests.RequestException, TypeError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "file inventory probe failed attempt=%d reason=%s",
                attempt + 1,
                type(exc).__name__,
            )
    failure_class = "timeout" if isinstance(last_error, requests.Timeout) else "error"
    return _DocumentInventoryProbe(
        (),
        failure_class=failure_class,
        failure_reason=(
            "file inventory timeout" if failure_class == "timeout" else "file inventory unavailable"
        ),
        attempts=2,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        timeout_s=timeout_s,
    )


def _unavailable_file_result(
    question: str,
    probe: _DocumentInventoryProbe,
) -> UploadedFileSearchResult:
    planned_lanes = _planned_file_lanes(question)
    reason = probe.failure_reason or "file inventory unavailable"
    source_items = tuple(
        {
            "planned_lane": lane,
            "retrieval_error": reason,
            "failure_class": probe.failure_class or "error",
            "inventory_attempts": probe.attempts,
            "inventory_elapsed_ms": round(probe.elapsed_ms, 3),
            "inventory_timeout_s": probe.timeout_s,
        }
        for lane in planned_lanes
    )
    return UploadedFileSearchResult(
        file_context="",
        file_sources=(),
        errors=(reason,),
        file_source_items=source_items,
        has_active_file=True,
        rag_query=_document_retrieval_question(question),
        rag_queries=(_document_retrieval_question(question),),
        rag_top_k=_file_search_limit(),
        rag_top_k_source="request",
    )


def _no_document_file_result(question: str) -> UploadedFileSearchResult:
    reason = "세션에 연결된 문서를 찾지 못했습니다."
    return UploadedFileSearchResult(
        file_context="",
        file_sources=(),
        errors=(reason,),
        file_source_items=tuple(
            {
                "planned_lane": lane,
                "no_document": True,
            }
            for lane in _planned_file_lanes(question)
        ),
        has_active_file=False,
    )


def _expired_document_file_result(
    question: str,
    documents: list[dict[str, Any]],
) -> UploadedFileSearchResult:
    notice = "업로드 문서가 만료되어 조회에서 제외되었습니다."
    expired_names = tuple(
        dict.fromkeys(
            str(document.get("file_name") or "업로드 문서").strip()
            for document in documents
        )
    )
    return UploadedFileSearchResult(
        file_context="",
        file_sources=expired_names,
        errors=(notice,),
        file_source_items=tuple(
            {
                "planned_lane": lane,
                "expired_document": True,
                "excluded_count": len(documents),
            }
            for lane in _planned_file_lanes(question)
        ),
        has_active_file=False,
        deterministic_answer=notice,
    )


def _document_is_expired(document: dict[str, Any], *, now: datetime | None = None) -> bool:
    if document.get("is_expired") is True:
        return True
    for key in ("expires_at", "expired_at", "expiration_date", "expiry"):
        raw = document.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            expires_at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= (now or datetime.now(UTC))
    return False


def _planned_file_lanes(question: str) -> tuple[str, ...]:
    if _should_prefetch_file_routes(question):
        return ("document_rag", "document_sql")
    document_target = _DOCUMENT_TARGET_RE.search(question) is not None
    spreadsheet_target = _SPREADSHEET_TARGET_RE.search(question) is not None
    if document_target and not spreadsheet_target:
        return ("document_rag",)
    if spreadsheet_target and not document_target:
        return ("document_sql",)
    return ("document_rag", "document_sql")


def _hydrate_sql_source_document_ids(
    raw_sources: list[Any],
    documents: list[dict[str, Any]],
) -> list[Any]:
    document_ids_by_name: dict[str, set[int]] = {}
    for document in documents:
        name = str(document.get("file_name") or "").strip()
        try:
            document_id = int(document["document_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if name and document_id > 0:
            document_ids_by_name.setdefault(name, set()).add(document_id)

    hydrated: list[Any] = []
    for source in raw_sources:
        if not isinstance(source, dict) or source.get("document_id") is not None:
            hydrated.append(source)
            continue
        candidate_ids = document_ids_by_name.get(str(source.get("file_name") or "").strip(), set())
        if len(candidate_ids) != 1:
            hydrated.append(source)
            continue
        hydrated.append({**source, "document_id": next(iter(candidate_ids))})
    return hydrated


def _join_contexts(*values: str) -> str:
    return "\n\n".join(value.strip() for value in values if value.strip())


def _merge_search_contexts(*values: str) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for value in values:
        parsed = _document_context_blocks(value)
        candidates = parsed or [value.strip()]
        for block in candidates:
            normalized = block.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                blocks.append(normalized)
    return "\n\n".join(blocks)


def _merge_search_materials(
    *materials: tuple[str, Any],
) -> tuple[str, list[Any], dict[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    seen: set[str] = set()
    input_count = 0
    for context, raw_sources in materials:
        blocks = _document_context_blocks(context)
        sources = raw_sources if isinstance(raw_sources, list) else []
        input_count += len(sources)
        if len(blocks) != len(sources):
            merged_context = _merge_search_contexts(
                *(value for value, _ in materials)
            )
            merged_sources = [
                source
                for _, group in materials
                if isinstance(group, list)
                for source in group
            ]
            return merged_context, merged_sources, {
                "source_alignment_preserved": False,
                "source_duplicates_removed": 0,
            }
        for block, source in zip(blocks, sources, strict=True):
            normalized = block.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            pairs.append((normalized, source))
    return (
        "\n\n".join(block for block, _ in pairs),
        [source for _, source in pairs],
        {
            "source_alignment_preserved": True,
            "source_duplicates_removed": input_count - len(pairs),
        },
    )


_DOCUMENT_TOKEN_STOPWORDS = frozenset(
    {"이", "문서", "에서", "중", "약물", "정리해줘", "알려줘", "보여줘"}
)


def _has_table_intent(question: str) -> bool:
    lowered = question.casefold()
    return any(
        marker in lowered
        for marker in ("정리", "목록", "몇 건", "표", "3상", "phase 3", "phase iii")
    )


def _document_core_tokens(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    tokens = [
        token
        for token in re.findall(r"[0-9a-z가-힣]+", lowered)
        if len(token) >= 2 and token not in _DOCUMENT_TOKEN_STOPWORDS
    ]
    if "3상" in lowered or "phase 3" in lowered or "phase iii" in lowered:
        tokens.extend(("3상", "phase 3", "phase iii"))
    return tuple(dict.fromkeys(tokens))


def _needs_document_retry(question: str, context: str) -> bool:
    if not _has_table_intent(question):
        return False
    if _phase_three_requested(question):
        return not _phase_three_table_present(context)
    lowered = context.casefold()
    tokens = _document_core_tokens(question)
    required = tokens[:2]
    return bool(required) and not any(token in lowered for token in required)


def _phase_three_requested(question: str) -> bool:
    return re.search(
        r"3\s*상|phase\s*(?:3|iii)",
        question,
        re.IGNORECASE,
    ) is not None


def _phase_three_present(context: str) -> bool:
    return re.search(
        r"3\s*상|phase\s*(?:3|iii)|\|\s*(?:<br>\s*)?iii\s*(?:<br>\s*)?\|",
        context,
        re.IGNORECASE,
    ) is not None


def _phase_three_table_present(context: str) -> bool:
    return re.search(
        r"(?im)^\s*\|[^\n]*\|\s*(?:<br>\s*)?"
        r"(?:iii|phase\s*(?:3|iii)|3\s*상)(?:\s*<br>)?\s*\|",
        context,
    ) is not None


def _document_supplement_query(
    question: str,
    *,
    context: str,
    raw_sources: Any,
) -> str:
    section, subject = _document_section_and_subject(question, context)
    phase = "Phase III" if _phase_three_requested(question) else ""
    if not subject and isinstance(raw_sources, list):
        subject = _document_subject_from_sources(raw_sources)
    parts = tuple(part for part in (section, subject, phase) if part)
    if parts:
        return " ".join(dict.fromkeys(parts))
    tokens = _document_core_tokens(question)
    return " ".join((*tokens[:5], "표", "목록"))


def _document_section_and_subject(question: str, context: str) -> tuple[str, str]:
    blocks = _document_context_blocks(context)
    if not blocks:
        return "", ""
    tokens = _document_core_tokens(question)
    def score(block: str) -> tuple[int, int, int]:
        section_match = re.search(r"(?:^|\n)\s*섹션:\s*([^\n]+)", block)
        section = section_match.group(1).casefold() if section_match else ""
        section_relevance = sum(
            marker in section
            for marker in ("pipeline", "drug", "treatment", "임상", "약물")
        )
        return (
            section_relevance,
            sum(token in block.casefold() for token in tokens),
            int("|" in block),
        )

    selected = max(blocks, key=score)
    section_match = re.search(r"(?:^|\n)\s*섹션:\s*([^\n]+)", selected)
    subject_match = re.search(
        r"(?:^|\n)\s*Disease\s+Analysis\s+([^\n]+)",
        selected,
        re.IGNORECASE,
    )
    return (
        section_match.group(1).strip() if section_match else "",
        subject_match.group(1).strip() if subject_match else "",
    )


def _document_subject_from_sources(raw_sources: list[Any]) -> str:
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        filename = str(source.get("file_name") or "").strip()
        stem = filename.rsplit(".", 1)[0]
        match = re.search(r"(?:^|[-_])([A-Z][A-Za-z]+)(?:[-_]\d{4}|$)", stem)
        if not match:
            continue
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", match.group(1)).strip()
    return ""


def _document_neighbor_trace(
    initial_sources: Any,
    supplemental_sources: Any,
    *,
    adjacent_window: int,
) -> dict[str, Any]:
    if not isinstance(initial_sources, list) or not isinstance(supplemental_sources, list):
        return {"adjacent_candidates_added": 0, "neighbor_pages_received": []}
    initial = {
        (str(source.get("file_name") or "").casefold(), _source_page(source))
        for source in initial_sources
        if isinstance(source, dict) and _source_page(source) is not None
    }
    neighbor_pages: list[int] = []
    for source in supplemental_sources:
        if not isinstance(source, dict):
            continue
        filename = str(source.get("file_name") or "").casefold()
        page = _source_page(source)
        if page is None:
            continue
        if any(
            filename == initial_filename
            and initial_page is not None
            and 0 < abs(page - initial_page) <= adjacent_window
            for initial_filename, initial_page in initial
        ):
            neighbor_pages.append(page)
    unique_pages = list(dict.fromkeys(neighbor_pages))
    return {
        "adjacent_candidates_added": len(neighbor_pages),
        "neighbor_pages_received": unique_pages,
    }


def _rerank_document_material(
    question: str,
    context: str,
    raw_sources: Any,
    *,
    adjacent_window: int,
) -> tuple[str, list[Any], dict[str, Any]]:
    if not context.strip() or not isinstance(raw_sources, list):
        return context, list(raw_sources) if isinstance(raw_sources, list) else [], {
            "reranked": False,
            "candidate_count": 0,
        }
    blocks = _document_context_blocks(context)
    if len(blocks) != len(raw_sources):
        return context, raw_sources, {"reranked": False, "candidate_count": len(blocks)}
    tokens = _document_core_tokens(question)
    lexical = [sum(1 for token in tokens if token in block.casefold()) for block in blocks]
    best_index = max(range(len(blocks)), key=lambda index: lexical[index]) if blocks else 0
    best_source = raw_sources[best_index] if best_index < len(raw_sources) else {}
    best_chunk = _optional_nonnegative_int(
        best_source.get("i_chunk_on_doc") if isinstance(best_source, dict) else None
    )
    best_page = _source_page(best_source)
    scored: list[tuple[int, int, str, Any]] = []
    for index, (block, source) in enumerate(zip(blocks, raw_sources, strict=True)):
        score = lexical[index] * 100
        if _phase_three_requested(question) and _phase_three_table_present(block):
            score += 700
        elif _phase_three_requested(question) and _phase_three_present(block):
            score += 100
        if _has_table_intent(question) and ("|" in block or "표" in block):
            score += 40
        chunk = _optional_nonnegative_int(
            source.get("i_chunk_on_doc") if isinstance(source, dict) else None
        )
        if best_chunk is not None and chunk is not None and abs(chunk - best_chunk) <= adjacent_window:
            score += 20
        if best_page is not None and _source_page(source) == best_page:
            score += 15
        scored.append((score, -index, block, source))
    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return (
        "\n\n".join(item[2] for item in scored),
        [item[3] for item in scored],
        {
            "reranked": True,
            "candidate_count": len(scored),
            "best_lexical_hits": lexical[best_index] if lexical else 0,
            "adjacent_window": adjacent_window,
        },
    )


def _document_context_blocks(context: str) -> list[str]:
    matches = list(re.finditer(r"(?m)^\[\d+\]\s", context))
    if not matches:
        return []
    return [
        context[match.start() : matches[index + 1].start()].strip()
        if index + 1 < len(matches)
        else context[match.start() :].strip()
        for index, match in enumerate(matches)
    ]


def _source_page(source: Any) -> int | None:
    if not isinstance(source, dict):
        return None
    return _optional_nonnegative_int(source.get("i_page") or source.get("page"))


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
    r"|(?:요약|핵심|결론).{0,16}(?:문서|보고서|파일|발표)"
    r"|^(?:(?:이\s*)?(?:내용\s*)?(?:요약|정리)(?:해\s*줘|해주세요|해줘요)?|내용\s*(?:을\s*)?알려\s*줘)[.!?]?$",
    re.IGNORECASE,
)
_TERSE_DOCUMENT_OVERVIEW_RE = re.compile(
    r"^(?:(?:이\s*)?(?:내용\s*)?(?:요약|정리)(?:해\s*줘|해주세요|해줘요)?|"
    r"내용\s*(?:을\s*)?알려\s*줘)[.!?]?$",
    re.IGNORECASE,
)
_DOCUMENT_REPRESENTATIVE_SAMPLE_QUERY = "문서 본문 핵심 수치 주요 결과"
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


def _document_retrieval_question(question: str) -> str:
    normalized = " ".join(question.split())
    if _TERSE_DOCUMENT_OVERVIEW_RE.fullmatch(normalized) is not None:
        return _DOCUMENT_REPRESENTATIVE_SAMPLE_QUERY
    return question


def _document_overview_top_k(question: str) -> int | None:
    if _DOCUMENT_OVERVIEW_QUESTION_RE.search(question) is None:
        return None
    try:
        configured = int(os.getenv("JW_CHAT_FILE_OVERVIEW_TOP_K", "8"))
    except ValueError:
        configured = 8
    return max(5, min(8, configured))


def _file_search_limit() -> int:
    try:
        configured = int(os.getenv("JW_CHAT_FILE_SEARCH_LIMIT", "20"))
    except ValueError:
        configured = 20
    return max(1, min(20, configured))


def _file_prompt_context_limit() -> int:
    try:
        configured = int(os.getenv("JW_CHAT_FILE_CONTEXT_MAX_CHARS", "24000"))
    except ValueError:
        configured = 24_000
    return max(1, min(24_000, configured))


def _bounded_file_prompt_context(context: str, limit: int) -> str:
    normalized = context.strip()
    if len(normalized) <= limit:
        return normalized
    sql_heading = "## 업로드 스프레드시트 조회 결과"
    if sql_heading in normalized:
        rag_context, sql_context = normalized.split(sql_heading, 1)
        sql_block = f"{sql_heading}\n{sql_context.strip()}"
        rag_budget = limit - len(sql_block) - 2
        if rag_budget > 0:
            bounded_rag = _bounded_complete_chunk_blocks(rag_context, rag_budget)
            if bounded_rag:
                return f"{bounded_rag}\n\n{sql_block}"
        return sql_block[:limit].rstrip()
    return _bounded_complete_chunk_blocks(normalized, limit)


def _bounded_complete_chunk_blocks(context: str, limit: int) -> str:
    blocks = [
        block.strip()
        for block in re.split(r"\n\n(?=\[\d+\]\s)", context.strip())
        if block.strip()
    ]
    selected: list[str] = []
    used = 0
    for block in blocks:
        separator = 2 if selected else 0
        if used + separator + len(block) > limit:
            break
        selected.append(block)
        used += separator + len(block)
    if selected:
        return "\n\n".join(selected)
    return context.strip()[:limit].rstrip()


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
    question: str,
) -> UploadedFileSearchResult | None:
    try:
        response = requests.get(
            f"{base_url}/documents",
            params={
                "workflow_id": workflow_id,
                "app_session_id": conversation_id,
                "chat_id": conversation_id,
            },
            headers=code_serving_actor_headers(),
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
        rag_query=_document_retrieval_question(question),
        rag_queries=(_document_retrieval_question(question),),
        rag_top_k=_file_search_limit(),
        rag_top_k_source="request",
    )
