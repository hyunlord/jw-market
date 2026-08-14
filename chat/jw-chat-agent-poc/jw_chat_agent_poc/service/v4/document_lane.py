from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import re
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
_TEMP_DOCUMENT_RE = re.compile(r"^TEMP_DOCUMENT_[^.]+(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)
_PARSER_DOCUMENT_PREFIX_RE = re.compile(
    r"^\[[^]]+\]\s*문서\s*:\s*TEMP_DOCUMENT_[^|]+\|\s*p\.\d+\s*",
    re.IGNORECASE,
)
_PICTURE_MARKER_RE = re.compile(r"<!--\s*(?:Start|End) of picture text\s*-->", re.IGNORECASE)
_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MARKDOWN_HEADING_ONLY_RE = re.compile(r"^#{1,6}\s+[^.!?。]+$")
_OVERVIEW_RE = re.compile(
    r"(?:pdf|문서|보고서|자료).{0,12}(?:설명|요약|개요|구성|핵심|결론)|"
    r"(?:설명|요약).{0,8}(?:pdf|문서|보고서|자료)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2}(?:[-./년]\s*\d{1,2})?(?:[-./월]\s*\d{1,2})?)")


def is_document_overview_question(question: str) -> bool:
    return bool(_OVERVIEW_RE.search(" ".join(question.split())))


def build_document_source_result(
    question: str,
    uploaded: UploadedFileSearchResult,
) -> SourceResult:
    records = _document_records(uploaded.file_context, uploaded.file_source_items)
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
        "used_chunk_count": sum(bool(str(record.get("content") or "").strip()) for record in records),
        "document_names": document_names,
        "raw_context_sha256": hashlib.sha256(uploaded.file_context.encode("utf-8")).hexdigest(),
        "raw_context_chars": len(uploaded.file_context),
        "errors": tuple(uploaded.errors),
    }
    return SourceResult(
        source="document",
        query=question,
        status="ok" if records else "empty",
        payload=payload,
        citations=citations,
        notice=("; ".join(uploaded.errors) if uploaded.errors else None),
    )


def render_document_overview(result: SourceResult) -> str:
    records = _mapping_records(result.payload)
    if not records:
        return "업로드 문서에서 설명에 사용할 수 있는 본문을 확인하지 못했습니다."
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
    matches = list(_CHUNK_HEADER_RE.finditer(context))
    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(context)
        raw_body = context[body_start:body_end].strip()
        section, lines = _section_and_lines(raw_body)
        chunks.append(
            {
                "chunk_index": int(match.group("index")),
                "document_name": match.group("name").strip(),
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
                "document_name": _first_document_name(source_items),
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
        page = chunk["page"] or _positive_int(item.get("i_page") or item.get("slide_number"))
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
        records.append(
            {
                "record_id": "DOC-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                "document_id": item.get("document_id"),
                "document_name": name,
                "section": section or None,
                "page": page,
                "content": content,
                "content_sha256": hashlib.sha256(chunk["raw_body"].encode("utf-8")).hexdigest(),
            }
        )
    return records


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


def _public_section(value: Any) -> str:
    section = " ".join(str(value or "").split()).strip(" :")
    return "" if section.casefold() in {"source", "section"} else section


def _overview_content_score(content: str) -> tuple[int, int]:
    return (0 if _MARKDOWN_HEADING_ONLY_RE.fullmatch(content) else 1, len(content))


def _source_item_for_chunk(
    chunk: Mapping[str, Any],
    source_items: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    name = str(chunk.get("document_name") or "").casefold()
    page = _positive_int(chunk.get("page"))
    for item in source_items:
        item_name = str(item.get("file_name") or "").casefold()
        item_page = _positive_int(item.get("i_page") or item.get("slide_number"))
        if name and item_name == name and (page is None or item_page == page):
            return item
    return source_items[0] if source_items else {}


def _first_document_name(items: Sequence[Mapping[str, Any]]) -> str:
    return str(items[0].get("file_name") or "업로드 문서") if items else "업로드 문서"


def _first_page(items: Sequence[Mapping[str, Any]]) -> int | None:
    return _positive_int(items[0].get("i_page") or items[0].get("slide_number")) if items else None


def _first_section(items: Sequence[Mapping[str, Any]]) -> str:
    return str(items[0].get("section_title") or "").strip() if items else ""


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _mapping_records(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("records"), list):
        return []
    return [record for record in payload["records"] if isinstance(record, Mapping)]


def _bounded_sentence(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
