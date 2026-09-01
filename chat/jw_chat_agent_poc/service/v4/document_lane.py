from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from jw_chat_agent_poc.service.file_search_client import UploadedFileSearchResult
from jw_chat_agent_poc.service.v4.contracts import Citation, SourceResult

_CHUNK_HEADER_RE = re.compile(
    r"(?m)^\[(?P<index>\d+)\]\s+(?P<name>.+?)"
    r"(?:\s+\((?:page|p)\s*=\s*(?P<page>\d+)\))?\s*$"
)
_SECTION_LABEL_RE = re.compile(r"^\s*섹션\s*:\s*(?P<section>.+?)\s*$", re.IGNORECASE)
_COPYRIGHT_RE = re.compile(r"^\s*(?:©|copyright\b).*$", re.IGNORECASE)
_PAGE_ONLY_RE = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.IGNORECASE)
_DOCUMENT_ID_SUFFIX_RE = re.compile(r"\s*\(document_id\s*=\s*[^)]+\)\s*", re.IGNORECASE)
_DOCUMENT_ID_RE = re.compile(r"\(document_id\s*=\s*(?P<value>[^)]+)\)", re.IGNORECASE)
_TEMP_DOCUMENT_RE = re.compile(r"^TEMP_DOCUMENT_[^.]+(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)
_PARSER_DOCUMENT_PREFIX_RE = re.compile(
    r"^\[[^]]+\]\s*문서\s*:\s*TEMP_DOCUMENT_[^|]+\|\s*p\.\d+\s*",
    re.IGNORECASE,
)
_PICTURE_MARKER_RE = re.compile(r"<!--\s*(?:Start|End) of picture text\s*-->", re.IGNORECASE)
_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MARKDOWN_HEADING_ONLY_RE = re.compile(r"^#{1,6}\s+[^.!?。]+$")
_SUMMARY_NAVIGATION_RE = re.compile(
    r"^(?:목차|차례|table\s+of\s+contents|contents|인사말|발간사|머리말|preface|foreword|cover)\b",
    re.IGNORECASE,
)
_SUMMARY_NAVIGATION_SECTION_RE = re.compile(
    r"(?:목차|차례|인사말|발간사|머리말|표지|table\s+of\s+contents|preface|foreword|cover)",
    re.IGNORECASE,
)
_SUMMARY_GREETING_RE = re.compile(
    r"(?:안녕하십니까|감사의\s*말씀|발간을\s*(?:축하|기념)|발표해\s*왔습니다)",
    re.IGNORECASE,
)
_SUMMARY_PROMOTIONAL_RE = re.compile(
    r"(?:powers\s+a\s+full\s+suite|all\s+rights\s+reserved|copyright\b)",
    re.IGNORECASE,
)
_SUMMARY_NUMERIC_FACT_RE = re.compile(
    r"\d[\d,.]*\s*(?:%|명|천명|만명|건|원|억원|년|개월)",
    re.IGNORECASE,
)
_OVERVIEW_RE = re.compile(
    r"(?:pdf|문서|보고서|리포트|자료).{0,12}(?:설명|요약|개요|구성|핵심|결론)|"
    r"(?:설명|요약).{0,8}(?:pdf|문서|보고서|리포트|자료)",
    re.IGNORECASE,
)
_TERSE_OVERVIEW_RE = re.compile(
    r"^(?:(?:이\s*)?(?:내용\s*)?(?:요약|정리)(?:해\s*줘|해주세요|해줘요)?|"
    r"내용\s*(?:을\s*)?알려\s*줘)[.!?]?$",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2}(?:[-./년]\s*\d{1,2})?(?:[-./월]\s*\d{1,2})?)")
_SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")
_RAG_CONTEXT_HEADING = "## 업로드 문서 검색 결과"
_SQL_CONTEXT_HEADING = "## 업로드 스프레드시트 조회 결과"
_CANONICAL_FILE_LANES = {
    "document_rag": "file_vdb",
    "document_sql": "file_sql",
}
_RELEVANCE_STOPWORDS = frozenset(
    {"알려줘", "알려주세요", "정리해줘", "내용", "관련", "현황", "업로드한", "파일"}
)
_DOCUMENT_RELEVANCE_DISTANCE_MAX_DEFAULT = 0.30


def is_document_overview_question(question: str) -> bool:
    normalized = " ".join(question.split())
    return bool(_OVERVIEW_RE.search(normalized) or _TERSE_OVERVIEW_RE.fullmatch(normalized))


def build_document_source_result(
    question: str,
    uploaded: UploadedFileSearchResult,
) -> SourceResult:
    record_source_items = uploaded.detail_source_items or uploaded.file_source_items
    record_context = uploaded.record_context or uploaded.file_context
    sql_execution_failure = _sql_execution_failure(uploaded)
    records = list(_document_records(record_context, record_source_items))
    if sql_execution_failure is not None:
        records = [
            record
            for record in records
            if document_record_lane(record) != "document_sql"
        ]
    summary_mode = is_document_overview_question(question)
    if summary_mode:
        records = _annotate_document_summary_records(records)
    deterministic_answer = uploaded.deterministic_answer.strip()
    retrieval_failures = tuple(
        item
        for item in uploaded.file_source_items
        if str(item.get("retrieval_error") or "").strip()
    )
    no_document_items = tuple(
        item for item in uploaded.file_source_items if item.get("no_document") is True
    )
    sql_detail = dict(uploaded.sql_detail)
    sql_record_index = next(
        (
            index
            for index, record in enumerate(records)
            if document_record_lane(record) == "document_sql"
        ),
        None,
    )
    if records and sql_detail:
        target_index = sql_record_index if sql_record_index is not None else 0
        records[target_index] = {
            **records[target_index],
            "sql_detail": sql_detail,
        }
    if deterministic_answer and sql_execution_failure is None:
        if records:
            target_index = sql_record_index if sql_record_index is not None else 0
            records[target_index] = {
                **records[target_index],
                "deterministic_answer": deterministic_answer,
                "sql_trace": tuple(uploaded.sql_trace),
                "sql_detail": sql_detail,
            }
        else:
            source_item = next(iter(uploaded.file_source_items), {})
            records.append(
                {
                    "document_name": str(
                        source_item.get("file_name") or "업로드 파일"
                    ),
                    "document_id": source_item.get("document_id"),
                    "sheet_name": source_item.get("sheet_name"),
                    "content": deterministic_answer,
                    "deterministic_answer": deterministic_answer,
                    "sql_trace": tuple(uploaded.sql_trace),
                    "sql_detail": sql_detail,
                }
            )
    document_names = tuple(
        dict.fromkeys(
            str(record.get("document_name") or "").strip()
            for record in records
            if str(record.get("document_name") or "").strip()
        )
    )
    citations = tuple(
        Citation(
            source=f"업로드 문서 · {name}",
            query=question,
            retrieved_at=datetime.now(UTC),
            used=True,
        )
        for name in document_names
    )
    payload = {
        "records": records,
        "returned_chunk_count": len(records),
        "used_chunk_count": sum(
            bool(str(record.get("content") or "").strip())
            and (not summary_mode or record.get("summary_input_eligible") is True)
            for record in records
        ),
        "document_names": document_names,
        "raw_context_sha256": hashlib.sha256(record_context.encode("utf-8")).hexdigest(),
        "raw_context_chars": len(record_context),
        "prompt_context_chars": len(uploaded.file_context),
        "prompt_context_limit": uploaded.prompt_context_limit,
        "rag_trace": dict(uploaded.rag_trace),
        "errors": tuple(uploaded.errors),
        "deterministic_answer": deterministic_answer,
        "sql_trace": tuple(uploaded.sql_trace),
        "route_accounting": _route_accounting(
            uploaded.file_source_items,
            records,
            sql_execution_failure=sql_execution_failure,
        ),
        "answer_eligible": _document_answer_eligible(question, records),
        "file_tool_details": {
            "document_rag": {
                "query": uploaded.rag_query or question,
                "queries": list(uploaded.rag_queries or (uploaded.rag_query or question,)),
                "top_k": uploaded.rag_top_k,
                "top_k_source": uploaded.rag_top_k_source or (
                    "response" if uploaded.rag_top_k is not None else "server_default"
                ),
                "retrieval_trace": dict(uploaded.rag_trace),
                "failure_reason": "; ".join(uploaded.errors),
            },
            "document_sql": dict(uploaded.sql_detail),
        },
    }
    failure_classes = {
        str(item.get("failure_class") or "error").casefold()
        for item in retrieval_failures
    }
    status = (
        "timeout"
        if "timeout" in failure_classes
        else "error"
        if retrieval_failures
        else "timeout"
        if sql_execution_failure is not None
        and sql_execution_failure["failure_class"] == "timeout"
        and not records
        else "error"
        if sql_execution_failure is not None and not records
        else "no_document"
        if no_document_items and not records
        else "ok"
        if records
        else "empty"
    )
    failure_reason = (
        "FILE_INVENTORY_TIMEOUT"
        if status == "timeout" and retrieval_failures
        else "FILE_INVENTORY_UNAVAILABLE"
        if status == "error"
        and retrieval_failures
        else "FILE_SQL_QUERY_TIMEOUT"
        if status == "timeout" and sql_execution_failure is not None
        else "FILE_SQL_QUERY_FAILED"
        if status == "error" and sql_execution_failure is not None
        else "NO_DOCUMENT"
        if status == "no_document"
        else None
    )
    notices = tuple(
        dict.fromkeys(
            [
                *uploaded.errors,
                *(
                    str(item.get("retrieval_error") or "").strip()
                    for item in retrieval_failures
                ),
            ]
        )
    )
    return SourceResult(
        source="document",
        query=question,
        status=status,
        payload=payload,
        citations=citations,
        notice=("; ".join(notices) if notices else None),
        failure_reason=failure_reason,
        failure_detail=(
            {
                "failure_class": status,
                "inventory_errors": list(notices),
            }
            if retrieval_failures
            else dict(sql_execution_failure)
            if sql_execution_failure is not None and not records
            else {"failure_class": "no_document"}
            if no_document_items
            else {}
        ),
    )


def document_record_lane(record: Mapping[str, Any]) -> str:
    explicit_lane = str(record.get("planned_lane") or "")
    if explicit_lane in {"document_rag", "document_sql"}:
        return explicit_lane
    route = str(record.get("retrieval_route") or "")
    if route in {"RAG 청크 검색", "파일 검색"}:
        return "document_rag"
    file_name = str(record.get("file_name") or record.get("document_name") or "").casefold()
    if route == "파일 SQL" or file_name.endswith(_SPREADSHEET_SUFFIXES):
        return "document_sql"
    return "document_rag"


def file_lane_id(record: Mapping[str, Any]) -> str:
    """Return the public file-lane identity without changing legacy tool names."""

    return _CANONICAL_FILE_LANES[document_record_lane(record)]


def canonical_file_lane(legacy_lane: str) -> str:
    return _CANONICAL_FILE_LANES[legacy_lane]


def _route_accounting(
    source_items: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    *,
    sql_execution_failure: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    planned = {"document_rag": False, "document_sql": False}
    inventory_failures: dict[str, dict[str, Any]] = {}
    for item in source_items:
        explicit_lane = str(item.get("planned_lane") or "")
        if explicit_lane in planned:
            lane = explicit_lane
        else:
            name = str(item.get("file_name") or "").casefold()
            lane = "document_sql" if name.endswith(_SPREADSHEET_SUFFIXES) else "document_rag"
        planned[lane] = True
        if str(item.get("retrieval_error") or "").strip():
            inventory_failures[lane] = {
                "failure_class": str(item.get("failure_class") or "error"),
                "reason": str(item.get("retrieval_error") or ""),
                "attempts": item.get("inventory_attempts"),
                "elapsed_ms": item.get("inventory_elapsed_ms"),
                "timeout_s": item.get("inventory_timeout_s"),
            }
        elif item.get("no_document") is True:
            inventory_failures[lane] = {"failure_class": "no_document"}
    return {
        lane: {
            "planned": is_planned,
            "returned_count": sum(document_record_lane(record) == lane for record in records),
            **(
                {"inventory_failure": inventory_failures[lane]}
                if lane in inventory_failures
                else {}
            ),
            **(
                {"execution_failure": dict(sql_execution_failure)}
                if lane == "document_sql" and sql_execution_failure is not None
                else {}
            ),
        }
        for lane, is_planned in planned.items()
    }


def _sql_execution_failure(
    uploaded: UploadedFileSearchResult,
) -> dict[str, str] | None:
    detail_error = str(uploaded.sql_detail.get("error") or "").strip()
    failed_trace = next(
        (
            item
            for item in reversed(uploaded.sql_trace)
            if str(item.get("status") or "").strip().casefold()
            in {"error", "failed", "query_failed", "timeout"}
        ),
        None,
    )
    if not detail_error and failed_trace is None:
        return None
    trace_reason = (
        str(failed_trace.get("reason") or "").strip()
        if failed_trace is not None
        else ""
    )
    reason = detail_error or trace_reason or "file SQL execution failed"
    normalized = reason.casefold()
    failure_class = (
        "timeout"
        if any(marker in normalized for marker in ("timeout", "timed out"))
        else "error"
    )
    return {
        "failure_class": failure_class,
        "reason": reason,
        "stage": (
            str(failed_trace.get("stage") or "sql")
            if failed_trace is not None
            else "sql"
        ),
    }


def _document_answer_eligible(
    question: str,
    records: Sequence[Mapping[str, Any]],
) -> bool:
    if records and is_document_overview_question(question):
        return True
    haystack = " ".join(str(record) for record in records).casefold()
    names = tuple(
        str(record.get("file_name") or record.get("document_name") or "").casefold()
        for record in records
    )
    normalized_question = question.casefold()
    if "팩트시트" in normalized_question and any(name.endswith(".pdf") for name in names):
        return True
    if (
        any(
            token in normalized_question
            for token in ("엑셀", "시트", "셀", "sellout", "sell out")
        )
        and any(name.endswith(_SPREADSHEET_SUFFIXES) for name in names)
    ):
        return True
    tokens = {
        token
        for token in re.findall(r"[0-9a-z가-힣]+", question.casefold())
        if len(token) >= 2 and token not in _RELEVANCE_STOPWORDS
    }
    if tokens and any(token in haystack for token in tokens):
        return True
    return _has_relevant_semantic_distance(records)


def _has_relevant_semantic_distance(records: Sequence[Mapping[str, Any]]) -> bool:
    raw_threshold = os.getenv(
        "FILE_DOCUMENT_RELEVANCE_DISTANCE_MAX",
        str(_DOCUMENT_RELEVANCE_DISTANCE_MAX_DEFAULT),
    )
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        threshold = _DOCUMENT_RELEVANCE_DISTANCE_MAX_DEFAULT
    if not math.isfinite(threshold) or threshold < 0:
        threshold = _DOCUMENT_RELEVANCE_DISTANCE_MAX_DEFAULT

    for record in records:
        score_kind = str(record.get("score_kind") or "").strip().casefold()
        if score_kind != "vector" and "distance" not in score_kind:
            continue
        try:
            distance = float(record.get("distance"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(distance) and 0 <= distance <= threshold:
            return True
    return False


def render_document_overview(result: SourceResult) -> str:
    records = _mapping_records(result.payload)
    if not records:
        return "수신된 상위 0개 청크에서는 설명에 사용할 수 있는 본문을 확인하지 못했습니다."
    summary_records = [
        record
        for record in records
        if record.get("summary_input_eligible") is not False
    ]
    if summary_records:
        records = summary_records
    names = tuple(
        dict.fromkeys(str(record.get("document_name") or "업로드 문서") for record in records)
    )
    sections = tuple(
        dict.fromkeys(
            (str(record.get("section") or "").strip(), _positive_int(record.get("page")))
            for record in records
            if str(record.get("section") or "").strip()
        )
    )
    contents = tuple(
        str(record.get("content") or "").strip()
        for record in records
        if str(record.get("content") or "").strip()
    )
    dates = tuple(dict.fromkeys(match.group(1) for content in contents for match in _DATE_RE.finditer(content)))
    overview_source = max(contents, key=_overview_content_score) if contents else ""
    overview = _bounded_sentence(overview_source, 500) if overview_source else "본문 요약을 확인하지 못했습니다."
    key_points = "\n".join(f"- {_bounded_sentence(content, 600)}" for content in contents[:5])
    structure = "\n".join(
        f"- {section}" + (f" (p.{page})" if page is not None else "")
        for section, page in sections
    ) or "- 문서의 섹션 정보는 검색 결과에 포함되지 않았습니다."
    date_text = " · ".join(dates[:5]) if dates else "검색된 본문에서 자료 기준일을 확인하지 못했습니다."
    sources = "\n".join(
        "- [출처: 업로드 문서 · "
        + str(record.get("document_name") or "업로드 문서")
        + (f" · {record['section']}" if record.get("section") else "")
        + (f" · p.{record['page']}" if _positive_int(record.get("page")) is not None else "")
        + "]"
        for record in records
    )
    return (
        "## 문서 개요\n"
        f"{', '.join(names)}은(는) {overview}\n\n"
        "## 문서 구성\n"
        f"{structure}\n\n"
        "## 핵심 내용\n"
        f"{key_points}\n\n"
        "## 자료 시점\n"
        f"{date_text}\n\n"
        "## 출처\n"
        f"{sources}"
    )


def _document_records(
    context: str,
    source_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rag_context, sql_context = _split_tool_contexts(context)
    if sql_context is None:
        return _records_from_context(context, source_items)
    rag_items = tuple(
        item
        for item in source_items
        if not str(item.get("file_name") or "").casefold().endswith(_SPREADSHEET_SUFFIXES)
    )
    sql_items = tuple(
        item
        for item in source_items
        if str(item.get("file_name") or "").casefold().endswith(_SPREADSHEET_SUFFIXES)
    )
    return [
        *_records_from_context(rag_context, rag_items),
        *_records_from_context(sql_context, sql_items),
    ]


def _split_tool_contexts(context: str) -> tuple[str, str | None]:
    if _SQL_CONTEXT_HEADING not in context:
        return context, None
    rag_context, sql_context = context.split(_SQL_CONTEXT_HEADING, 1)
    return (
        rag_context.replace(_RAG_CONTEXT_HEADING, "", 1).strip(),
        sql_context.strip(),
    )


def _records_from_context(
    context: str,
    source_items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    matches = list(_CHUNK_HEADER_RE.finditer(context))
    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(context)
        raw_body = context[body_start:body_end].strip()
        section, lines = _section_and_lines(raw_body)
        raw_name = match.group("name").strip()
        chunks.append(
            {
                "chunk_index": int(match.group("index")),
                "source_position": index,
                "document_name": raw_name,
                "document_id": _document_id_from_header(raw_name),
                "page": _positive_int(match.group("page")),
                "section": section,
                "raw_body": raw_body,
                "lines": lines,
            }
        )
    if not chunks and context.strip():
        chunks.append(
            {
                "chunk_index": 1,
                "source_position": 0,
                "document_name": _first_document_name(source_items),
                "document_id": None,
                "page": _first_page(source_items),
                "section": _first_section(source_items),
                "raw_body": context.strip(),
                "lines": _section_and_lines(context.strip())[1],
            }
        )
    repeated_lines = Counter(
        line.casefold()
        for chunk in chunks
        for line in chunk["lines"]
        if len(line) >= 8
    )
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        item = _source_item_for_chunk(chunk, source_items)
        section = _public_section(
            chunk["section"] or str(item.get("section_title") or "").strip()
        )
        page = chunk["page"] or _positive_int(
            item.get("i_page") or item.get("page") or item.get("slide_number")
        )
        name = _public_document_name(
            chunk["document_name"],
            fallback=str(item.get("file_name") or _first_document_name(source_items)),
        )
        visible_lines = [
            line
            for line in chunk["lines"]
            if repeated_lines[line.casefold()] <= 1
        ]
        content = " ".join(visible_lines).strip()[:4000]
        if not content:
            continue
        identity = "|".join((name, str(page or ""), section, str(chunk["chunk_index"])))
        retrieval_route = _public_retrieval_route(item)
        records.append(
            {
                "record_id": "DOC-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                "document_id": item.get("document_id") or chunk.get("document_id"),
                "file_name": name,
                "document_name": name,
                "file_type": _public_file_type(name),
                "section": section or None,
                "page": page,
                "content": content,
                "content_excerpt": _bounded_sentence(content, 320),
                "retrieval_route": retrieval_route,
                "sheet_name": item.get("sheet_name"),
                "row_start": item.get("row_start"),
                "row_end": item.get("row_end"),
                "columns": item.get("columns") or _markdown_columns(chunk["raw_body"]),
                "result_excerpt": (
                    item.get("result_excerpt")
                    or (_bounded_sentence(content, 320) if retrieval_route == "파일 SQL" else None)
                ),
                "content_sha256": hashlib.sha256(chunk["raw_body"].encode("utf-8")).hexdigest(),
                "chunk_id": item.get("chunk_id"),
                "source_chunk_index": item.get("i_chunk_on_doc"),
                "score": item.get("score"),
                "score_kind": item.get("score_kind"),
                "similarity_score": (
                    item.get("similarity_score")
                    if item.get("similarity_score") is not None
                    else item.get("score")
                ),
                "distance": item.get("distance"),
            }
        )
    return records


def _annotate_document_summary_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    seen_body: set[str] = set()
    for raw_record in records:
        record = dict(raw_record)
        reason = _summary_chunk_exclusion_reason(record, seen_body)
        eligible = reason is None
        normalized = " ".join(str(record.get("content") or "").casefold().split())
        if eligible:
            seen_body.add(normalized)
        record.update(
            {
                "summary_mode": True,
                "summary_input_eligible": eligible,
                "summary_exclusion_reason": reason,
            }
        )
        annotated.append(record)
    return annotated


def _summary_chunk_exclusion_reason(
    record: Mapping[str, Any],
    seen_body: set[str],
) -> str | None:
    if document_record_lane(record) != "document_rag":
        return "non_document_chunk"
    content = " ".join(str(record.get("content") or "").split())
    normalized = content.casefold()
    section = " ".join(str(record.get("section") or "").split())
    if not normalized:
        return "empty"
    if normalized in seen_body:
        return "duplicate"
    if _SUMMARY_NAVIGATION_RE.search(normalized) is not None:
        return "navigation"
    if _SUMMARY_NAVIGATION_SECTION_RE.search(section) is not None:
        return "navigation_section"
    if _SUMMARY_GREETING_RE.search(normalized) is not None:
        return "greeting"
    if _SUMMARY_PROMOTIONAL_RE.search(normalized) is not None:
        return "promotional"
    lines = tuple(
        line.strip()
        for line in str(record.get("content") or "").splitlines()
        if line.strip()
    )
    if lines and sum(line.startswith("|") for line in lines) >= 2:
        return "table_fragment"
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", content)
    if _positive_int(record.get("page")) != 1 and (
        _MARKDOWN_HEADING_ONLY_RE.fullmatch(content) is not None
        or (
            len(tokens) <= 12
            and _SUMMARY_NUMERIC_FACT_RE.search(content) is None
            and not re.search(r"[.!?。]", content)
        )
    ):
        return "cover_or_title"
    return None


def _section_and_lines(raw_body: str) -> tuple[str, list[str]]:
    section = ""
    lines: list[str] = []
    for raw_line in raw_body.splitlines():
        line = _clean_display_line(raw_line)
        if not line:
            continue
        section_match = _SECTION_LABEL_RE.match(line)
        if section_match:
            section = section_match.group("section").strip()
            continue
        if _COPYRIGHT_RE.match(line) or _PAGE_ONLY_RE.match(line):
            continue
        lines.append(line)
    return section, lines


def _clean_display_line(raw_line: str) -> str:
    line = _PICTURE_MARKER_RE.sub(" ", raw_line)
    line = _HTML_BREAK_RE.sub(" ", line)
    line = _PARSER_DOCUMENT_PREFIX_RE.sub("", line)
    return " ".join(line.split())


def _public_document_name(value: Any, *, fallback: str) -> str:
    name = _DOCUMENT_ID_SUFFIX_RE.sub(" ", str(value or "")).strip()
    if not name or _TEMP_DOCUMENT_RE.fullmatch(name):
        name = _DOCUMENT_ID_SUFFIX_RE.sub(" ", fallback).strip()
    return name or "업로드 문서"


def _document_id_from_header(value: str) -> str | None:
    match = _DOCUMENT_ID_RE.search(value)
    if match is None:
        return None
    document_id = " ".join(match.group("value").split())
    return document_id or None


def _public_file_type(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].upper() if "." in name else ""
    known = {"PDF", "XLS", "XLSX", "PPT", "PPTX", "DOC", "DOCX"}
    return suffix if suffix in known else "원천 미제공"


def _public_retrieval_route(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("retrieval_route") or "").strip()
    if explicit in {"RAG 청크 검색", "파일 SQL"}:
        return explicit
    if item.get("source_channel") or item.get("i_page") or item.get("slide_number"):
        return "RAG 청크 검색"
    if item.get("sheet_name"):
        return "파일 SQL"
    return "RAG 청크 검색"


def _markdown_columns(raw_body: str) -> tuple[str, ...] | None:
    for line in raw_body.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        columns = tuple(part.strip() for part in stripped.strip("|").split("|") if part.strip())
        if columns and not all(set(column) <= {"-", ":"} for column in columns):
            return columns
    return None


def _public_section(value: Any) -> str:
    section = " ".join(str(value or "").split()).strip(" :")
    return "" if section.casefold() in {"source", "section"} else section


def _overview_content_score(content: str) -> tuple[int, int]:
    return (0 if _MARKDOWN_HEADING_ONLY_RE.fullmatch(content) else 1, len(content))


def _source_item_for_chunk(
    chunk: Mapping[str, Any],
    source_items: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    raw_name = str(chunk.get("document_name") or "")
    name = _public_document_name(raw_name, fallback=raw_name).casefold()
    page = _positive_int(chunk.get("page"))
    source_position = _positive_or_zero_int(chunk.get("source_position"))
    if source_position is not None and source_position < len(source_items):
        candidate = source_items[source_position]
        candidate_name = str(candidate.get("file_name") or "").casefold()
        candidate_page = _positive_int(
            candidate.get("i_page")
            or candidate.get("page")
            or candidate.get("slide_number")
        )
        if name and candidate_name == name and (page is None or candidate_page == page):
            return candidate
    for item in source_items:
        item_name = str(item.get("file_name") or "").casefold()
        item_page = _positive_int(
            item.get("i_page") or item.get("page") or item.get("slide_number")
        )
        if name and item_name == name and (page is None or item_page == page):
            return item
    return source_items[0] if source_items else {}


def _first_document_name(items: Sequence[Mapping[str, Any]]) -> str:
    return str(items[0].get("file_name") or "업로드 문서") if items else "업로드 문서"


def _first_page(items: Sequence[Mapping[str, Any]]) -> int | None:
    return (
        _positive_int(
            items[0].get("i_page")
            or items[0].get("page")
            or items[0].get("slide_number")
        )
        if items
        else None
    )


def _first_section(items: Sequence[Mapping[str, Any]]) -> str:
    return str(items[0].get("section_title") or "").strip() if items else ""


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_or_zero_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _mapping_records(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("records"), list):
        return []
    return [record for record in payload["records"] if isinstance(record, Mapping)]


def _bounded_sentence(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
