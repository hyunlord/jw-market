from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import re
from typing import Any

from jw_chat_agent_poc.service.v4.evidence_payload import (
    is_request_metadata_key,
    is_url_payload_key,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet
from jw_chat_agent_poc.service.v4.markdown_fences import (
    FenceState,
    advance_fence_state,
)
from jw_chat_agent_poc.service.v4.retrieval_events import (
    RetrievalEvent,
    public_retrieval_notice,
)
from jw_chat_agent_poc.service.v4.source_labels import SOURCE_LABELS


_ENTITY_PATTERNS = (
    re.compile(r"\bNCT\d{8}\b", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}[A-Z0-9]*-\d+[A-Za-z]?(?![A-Za-z0-9])"),
    re.compile(r"\b[A-Z][A-Za-z]+(?:\s+[A-Za-z][A-Za-z0-9-]+)+\b"),
    re.compile(r"[가-힣A-Za-z0-9]{2,}(?:제약|바이오|약품|헬스케어)"),
)
_TABLE_DELIMITER_RE = re.compile(
    r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$"
)
_HEADING_RE = re.compile(r"^(#{2,6})\s+\S")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_CORE_HEADINGS = frozenset({"핵심 답", "핵심 요약"})
_CORE_RECOVERY_ORDER = ("근거와 맥락", "종합 인사이트")
_URL_TOKEN_RE = re.compile(r'''https?://[^\s<>\[\]()"']+''')
_GENERIC_TABLE_HEADER_ENTITIES = frozenset(
    {
        "nct id",
        "brief title",
        "completion date",
        "intervention name",
        "intervention type",
        "lead sponsor",
        "official title",
        "overall status",
        "primary completion date",
        "start date",
        "study type",
    }
)


def sanitize_bound_surface(
    question: str,
    answer: str,
    evidence_sets: Sequence[EvidenceSet],
    retrieval_events: Sequence[RetrievalEvent],
) -> tuple[str, dict[str, Any]]:
    corpus = " ".join(
        (
            question,
            *(label for label in SOURCE_LABELS.values()),
            *(_payload_text(record.payload) for item in evidence_sets for record in item.records),
            *(
                f"{ref.title or ''} {ref.url}"
                for item in evidence_sets
                for ref in item.source_refs
            ),
            *(
                f"{ref.title or ''} {ref.url}"
                for item in evidence_sets
                for record in item.records
                for ref in record.source_refs
            ),
            *(event.entity_id or "" for event in retrieval_events),
        )
    ).casefold()
    bound_notice_lines = frozenset(
        f"- [확인 한계] {public_retrieval_notice(event, label=SOURCE_LABELS.get(event.tool))}"
        for event in retrieval_events
    )
    allowed_urls = _allowed_evidence_urls(evidence_sets)
    output: list[str] = []
    removed_hashes: list[str] = []
    removed_source_hashes: list[str] = []
    answer_lines = answer.splitlines()
    fence_state: FenceState | None = None
    for line_index, line in enumerate(answer_lines):
        fence_state, is_fence_boundary = advance_fence_state(fence_state, line)
        if is_fence_boundary:
            output.append(line)
            continue
        if fence_state is not None:
            output.append(line)
            continue
        if line.strip() in bound_notice_lines:
            output.append(line)
            continue
        if _contains_unbound_url(line, allowed_urls):
            line_hash = sha256(line.encode("utf-8")).hexdigest()
            removed_hashes.append(line_hash)
            removed_source_hashes.append(line_hash)
            continue
        if _is_structural_line(line):
            output.append(line)
            continue
        candidates = tuple(
            dict.fromkeys(
                (
                    *(
                        match.group(0).strip()
                        for pattern in _ENTITY_PATTERNS
                        for match in pattern.finditer(line)
                    ),
                    *_claimed_query_entities(line),
                )
            )
        )
        if _is_markdown_table_header(answer_lines, line_index):
            candidates = tuple(
                item
                for item in candidates
                if item.casefold() not in _GENERIC_TABLE_HEADER_ENTITIES
            )
        unsupported = tuple(item for item in candidates if item.casefold() not in corpus)
        if unsupported:
            removed_hashes.append(sha256(line.encode("utf-8")).hexdigest())
            continue
        output.append(line)
    sanitized, removed_empty_tables = _prune_empty_tables("\n".join(output))
    sanitized, removed_empty_headings = _prune_empty_sections(sanitized)
    original_had_core = any(
        match.group(1).strip() in _CORE_HEADINGS
        for line in answer_lines
        if (match := _H2_RE.match(line.strip())) is not None
    )
    sanitized, recovered_from = _recover_core_section(
        sanitized,
        original_had_core=original_had_core,
    )
    return sanitized, {
        "answer_mutation": sanitized != answer,
        "removed_unbound_lines": len(removed_hashes),
        "removed_line_sha256": removed_hashes,
        "removed_unbound_source_lines": len(removed_source_hashes),
        "removed_source_line_sha256": removed_source_hashes,
        "removed_empty_tables": removed_empty_tables,
        "removed_empty_headings": removed_empty_headings,
        "core_section_recovered_from": recovered_from,
    }


def _is_structural_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("```")
        or bool(_TABLE_DELIMITER_RE.fullmatch(line))
    )


def _is_markdown_table_header(lines: Sequence[str], index: int) -> bool:
    return (
        _is_table_line(lines[index])
        and index + 1 < len(lines)
        and bool(_TABLE_DELIMITER_RE.fullmatch(lines[index + 1]))
    )


def _payload_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(
            f"{key} {_payload_text(item)}"
            for key, item in value.items()
            if not is_request_metadata_key(str(key))
        )
    if isinstance(value, (list, tuple)):
        return " ".join(_payload_text(item) for item in value)
    return str(value or "")


def _allowed_evidence_urls(
    evidence_sets: Sequence[EvidenceSet],
) -> tuple[str, ...]:
    values: list[str] = []
    for item in evidence_sets:
        values.extend(ref.url for ref in item.source_refs)
        for record in item.records:
            values.extend(ref.url for ref in record.source_refs)
            values.extend(_payload_urls(record.payload))
    return tuple(sorted(dict.fromkeys(values), key=len, reverse=True))


def _payload_urls(value: Any, *, field_name: str | None = None) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            url
            for key, item in value.items()
            if not is_request_metadata_key(str(key), include_url_fields=False)
            for url in _payload_urls(item, field_name=str(key).casefold())
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            url for item in value for url in _payload_urls(item, field_name=field_name)
        )
    text = str(value or "").strip()
    if field_name is not None and is_url_payload_key(field_name) and text.startswith(
        ("http://", "https://")
    ):
        return (text,)
    return ()


def _urls_in_text(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).rstrip(".,;:") for match in _URL_TOKEN_RE.finditer(value))


def _contains_unbound_url(line: str, allowed_urls: Sequence[str]) -> bool:
    allowed = frozenset(allowed_urls)
    return any(url not in allowed for url in _urls_in_text(line))


def _claimed_query_entities(line: str) -> tuple[str, ...]:
    match = re.search(r"질의에\s*포함된\s+(.+?)\s+관련", line)
    if match is None:
        return ()
    return tuple(
        value.strip(" ,·")
        for value in re.split(r"\s+(?:및|와|과)\s+|\s*[,，·]\s*", match.group(1))
        if value.strip(" ,·")
    )


def _prune_empty_tables(value: str) -> tuple[str, int]:
    lines = value.splitlines()
    output: list[str] = []
    removed = 0
    index = 0
    fence_state: FenceState | None = None
    while index < len(lines):
        fence_state, is_fence_boundary = advance_fence_state(
            fence_state,
            lines[index],
        )
        if is_fence_boundary:
            output.append(lines[index])
            index += 1
            continue
        if fence_state is not None:
            output.append(lines[index])
            index += 1
            continue
        if not _is_table_line(lines[index]):
            output.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and _is_table_line(lines[end]):
            end += 1
        block = lines[index:end]
        is_complete = (
            len(block) >= 3
            and bool(_TABLE_DELIMITER_RE.fullmatch(block[1]))
            and any(line.strip(" |") for line in block[2:])
        )
        if is_complete:
            output.extend(block)
        else:
            removed += 1
        index = end
    return "\n".join(output), removed


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def _prune_empty_sections(value: str) -> tuple[str, int]:
    lines = value.splitlines()
    headings = tuple(
        (index, len(match.group(1)))
        for index, match in _headings_outside_fences(lines, _HEADING_RE)
    )
    removed: set[int] = set()
    removed_heading_count = 0
    for position, (index, level) in enumerate(headings):
        end = len(lines)
        for next_index, next_level in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        has_body = any(_is_substantive_section_line(line) for line in lines[index + 1 : end])
        if not has_body:
            removed.update(range(index, end))
            removed_heading_count += 1

    output: list[str] = []
    for index, line in enumerate(lines):
        if index in removed:
            continue
        if not line.strip() and (not output or not output[-1].strip()):
            continue
        output.append(line)
    return "\n".join(output).strip(), removed_heading_count


def _is_substantive_section_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _HEADING_RE.match(stripped) is not None:
        return False
    return re.fullmatch(r"\*\*[^*]+\*\*", stripped) is None


def prune_empty_surface_sections(value: str) -> tuple[str, int]:
    return _prune_empty_sections(value)


def _recover_core_section(
    value: str,
    *,
    original_had_core: bool,
) -> tuple[str, str | None]:
    lines = value.splitlines()
    sections = tuple(
        (index, match.group(1).strip())
        for index, match in _headings_outside_fences(lines, _H2_RE)
    )
    if any(heading in _CORE_HEADINGS for _, heading in sections):
        return value, None

    if original_had_core:
        for recovery_heading in _CORE_RECOVERY_ORDER:
            for position, (start, heading) in enumerate(sections):
                if heading != recovery_heading:
                    continue
                end = sections[position + 1][0] if position + 1 < len(sections) else len(lines)
                body = "\n".join(lines[start + 1 : end]).strip()
                if not body:
                    continue
                remaining = "\n".join((*lines[:start], *lines[end:])).strip()
                core = f"## 핵심 답\n{body}"
                return core + (f"\n\n{remaining}" if remaining else ""), heading
        return value, None

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if (heading_match := _H2_RE.match(line.strip())) is not None:
            end = sections[1][0] if len(sections) > 1 else len(lines)
            body = "\n".join(lines[index + 1 : end]).strip()
            if not body:
                continue
            remaining = "\n".join((*lines[:index], *lines[end:])).strip()
            core = f"## 핵심 답\n{body}"
            return (
                core + (f"\n\n{remaining}" if remaining else ""),
                heading_match.group(1).strip(),
            )
        if _HEADING_RE.match(line.strip()) is not None:
            continue
        return "\n".join((*lines[:index], "## 핵심 답", *lines[index:])), "answer_lead"
    return value, None


def _headings_outside_fences(
    lines: Sequence[str],
    pattern: re.Pattern[str],
) -> tuple[tuple[int, re.Match[str]], ...]:
    values: list[tuple[int, re.Match[str]]] = []
    fence_state: FenceState | None = None
    for index, line in enumerate(lines):
        fence_state, is_fence_boundary = advance_fence_state(fence_state, line)
        if is_fence_boundary:
            continue
        if fence_state is not None:
            continue
        match = pattern.match(line.strip())
        if match is not None:
            values.append((index, match))
    return tuple(values)
