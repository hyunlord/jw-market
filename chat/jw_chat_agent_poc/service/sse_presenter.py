from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Protocol
from uuid import UUID

from jw_chat_agent_poc.service.sse_protocol import (
    iter_markdown_sse_events,
    strip_structured_markdown_tables,
)

SSE_PRESENTER_ENV: Final = "JW_CHAT_SSE_PRESENTER_ENABLED"
_SECTION_HEADING_RE: Final = re.compile(r"(?m)^## ([^\n]+)\n")
_FINAL_LABEL_RE: Final = re.compile(
    r"^\s*(?:\*\*)?\[(가설|해석)(?:\s+\d+)?\](?:\*\*)?\s*",
    re.MULTILINE,
)


def normalize_section_labels(text: str) -> str:
    """Number and bold section labels at every release boundary."""

    counts = {"가설": 0, "해석": 0}

    def replacement(match: re.Match[str]) -> str:
        label = match.group(1)
        counts[label] += 1
        return f"**[{label} {counts[label]}]** "

    return _FINAL_LABEL_RE.sub(replacement, text)


def normalize_section_label_fragments(texts: Sequence[str]) -> list[str]:
    """Number labels continuously across independently emitted paragraphs."""

    counts = {"가설": 0, "해석": 0}

    def normalize(text: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            label = match.group(1)
            counts[label] += 1
            return f"**[{label} {counts[label]}]** "

        return _FINAL_LABEL_RE.sub(replacement, text)

    return [normalize(text) for text in texts]


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
    timing: Mapping[str, Any],
    trace: Mapping[str, Any],
    streamed_prefix: str = "",
    streamed_section_ids: Sequence[str] = (),
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
    raw_answer_sections = _answer_sections_payload(trace)
    raw_section_table_texts = (
        tuple(raw_answer_sections["content"].values())
        if raw_answer_sections is not None
        else ()
    )
    trace = normalize_structured_answer_trace(text, trace)
    answer_sections = _answer_sections_payload(trace)
    if answer_sections is not None:
        # The streaming path already announced the section contract. Re-emitting
        # pending metadata here replaces completed portal state with empty sections.
        if not streamed_prefix and not streamed_section_ids:
            yield sse_json_event(
                "answer_sections",
                {
                    "schema": answer_sections["schema"],
                    "sections": answer_sections["sections"],
                    "evidence_catalog": answer_sections["evidence_catalog"],
                },
            )
        table_texts: list[str] = []
        semantic_realization = trace.get("semantic_realization")
        if isinstance(semantic_realization, Mapping):
            semantic_text = semantic_realization.get("text")
            if isinstance(semantic_text, str) and semantic_text.strip():
                table_texts.append(semantic_text)
        table_texts.append(text)
        table_texts.extend(
            section_text
            for section_text in raw_section_table_texts
            if isinstance(section_text, str) and section_text.strip()
        )
        emitted_table_events: set[str] = set()
        for table_text in table_texts:
            for event in iter_markdown_sse_events(
                table_text,
                emit_legacy_table_blocks=False,
            ):
                if not event.startswith("event: tables\n"):
                    continue
                if event in emitted_table_events:
                    continue
                emitted_table_events.add(event)
                yield event
        if charts:
            yield sse_json_event("charts", charts)
        content = answer_sections["content"]
        paragraphs = answer_sections.get("paragraphs", {})
        emitted_sections = {str(section_id) for section_id in streamed_section_ids}
        for section in sorted(
            answer_sections["sections"], key=lambda item: int(item["order"])
        ):
            section_id = str(section["id"])
            if section_id in emitted_sections:
                continue
            section_paragraphs = paragraphs.get(section_id, ())
            if section_paragraphs:
                cleaned_paragraphs = []
                for paragraph in section_paragraphs:
                    cleaned = strip_structured_markdown_tables(str(paragraph["text"]))
                    if cleaned:
                        cleaned_paragraphs.append({**paragraph, "text": cleaned})
                for index, paragraph in enumerate(cleaned_paragraphs):
                    paragraph_start = bool(paragraph.get("paragraph_start", index == 0))
                    payload = {
                        "schema": "jw.answer-section-delta.v1",
                        "section_id": section_id,
                        "delta": (
                            ""
                            if index == 0
                            else "\n\n"
                            if paragraph_start
                            else " "
                        )
                        + str(paragraph["text"]),
                        "status": (
                            "complete"
                            if index == len(cleaned_paragraphs) - 1
                            else "streaming"
                        ),
                    }
                    evidence = paragraph.get("evidence", ())
                    if evidence:
                        payload["evidence"] = evidence
                    evidence_group = paragraph.get("evidence_group")
                    if evidence_group:
                        payload["evidence_group"] = evidence_group
                    yield sse_json_event("answer_section_delta", payload)
                if not cleaned_paragraphs:
                    yield sse_json_event(
                        "answer_section_delta",
                        {
                            "schema": "jw.answer-section-delta.v1",
                            "section_id": section_id,
                            "delta": "",
                            "status": "complete",
                        },
                    )
            else:
                yield sse_json_event(
                    "answer_section_delta",
                    {
                        "schema": "jw.answer-section-delta.v1",
                        "section_id": section_id,
                        "delta": strip_structured_markdown_tables(
                            str(content[section_id])
                        ),
                        "status": "complete",
                    },
                )
        yield sse_json_event("timing", timing)
        yield sse_json_event("trace", trace)
        yield "event: done\ndata: ok\n\n"
        return
    remaining_text = _remaining_after_streamed_prefix(text, streamed_prefix)
    if remaining_text:
        yield from iter_markdown_sse_events(remaining_text)
    if charts:
        yield sse_json_event("charts", charts)
    yield sse_json_event("timing", timing)
    yield sse_json_event("trace", trace)
    yield "event: done\ndata: ok\n\n"


_DIRECT_RELATED_CAPTION_RE: Final = re.compile(
    r"직접 관련\s+(?P<count>[\d,]+)건\s+기준"
)
_DIRECT_RELATED_BODY_RE: Final = re.compile(
    r"(?P<prefix>직접 관련(?:된)?\s+(?:임상(?:시험)?|특허)?(?:은|는|이|가)?\s*)"
    r"(?P<count>[\d,]+)(?P<suffix>건)"
)
_PRODUCT_PATENT_CAPTION_RE: Final = re.compile(
    r"(?:제품특허 조합|제품특허)\s+(?P<count>[\d,]+)건"
)
_PRODUCT_PATENT_BODY_RE: Final = re.compile(
    r"(?P<prefix>(?:총\s+)?)(?P<count>[\d,]+)"
    r"(?P<suffix>건(?:의)?\s+(?:제품 관련 특허|제품특허)|건\s+기준)"
)
_TABLE_ROW_COUNT_RE: Final = re.compile(
    r"^\|\s*(?P<label>[^|]+?)\s*\|\s*(?P<count>[\d,]+)(?:건)?\s*\|",
    re.MULTILINE,
)
_DISTRIBUTION_HEADINGS: Final = {
    "상태 분포": "status",
    "단계 분포": "phase",
    "주관 스폰서 상위": "sponsor",
}


def _distribution_tables(text: str) -> dict[str, dict[str, Any]]:
    """Parse each deterministic distribution table within its own boundary."""

    lines = text.splitlines()
    result: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(lines):
        heading = lines[index].lstrip("# ").strip()
        distribution = _DISTRIBUTION_HEADINGS.get(heading)
        if distribution is None:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("### "):
            end += 1
        block = "\n".join(lines[index:end])
        caption = _DIRECT_RELATED_CAPTION_RE.search(block)
        counts = {
            match.group("label").strip(): int(match.group("count").replace(",", ""))
            for match in _TABLE_ROW_COUNT_RE.finditer(block)
            if match.group("label").strip() not in {"항목", "---"}
        }
        if caption and counts:
            result[distribution] = {
                "population": int(caption.group("count").replace(",", "")),
                "counts": counts,
                "sum": sum(counts.values()),
            }
        index = end
    return result


def _table_authority(table_texts: Sequence[str]) -> dict[str, Any]:
    authority: dict[str, Any] = {"distributions": {}}
    for text in table_texts:
        if "direct_related" not in authority and (
            match := _DIRECT_RELATED_CAPTION_RE.search(text)
        ):
            authority["direct_related"] = int(
                match.group("count").replace(",", "")
            )
        if "product_patents" not in authority and (
            match := _PRODUCT_PATENT_CAPTION_RE.search(text)
        ):
            authority["product_patents"] = int(
                match.group("count").replace(",", "")
            )
        for name, distribution in _distribution_tables(text).items():
            authority["distributions"].setdefault(name, distribution)
    populations = {
        int(distribution["population"])
        for distribution in authority["distributions"].values()
    }
    if len(populations) == 1:
        authority["direct_related"] = next(iter(populations))
    authority["distribution_sum_mismatch"] = sum(
        int(distribution["sum"] != distribution["population"])
        for distribution in authority["distributions"].values()
    )
    authority["sigma_checked_count"] = len(authority["distributions"])
    return authority


def _align_direct_related_count(text: str, count: int) -> tuple[str, int]:
    display = f"{count:,}"
    return _DIRECT_RELATED_BODY_RE.subn(
        lambda match: f"{match.group('prefix')}{display}{match.group('suffix')}",
        text,
    )


def _align_table_authority(
    text: str,
    authority: Mapping[str, Any],
) -> tuple[str, int]:
    updated = text
    corrections = 0
    direct_related = authority.get("direct_related")
    if isinstance(direct_related, int):
        updated, count = _align_direct_related_count(updated, direct_related)
        corrections += count
    product_patents = authority.get("product_patents")
    if isinstance(product_patents, int):
        updated, count = _PRODUCT_PATENT_BODY_RE.subn(
            lambda match: (
                f"{match.group('prefix')}{product_patents:,}{match.group('suffix')}"
            ),
            updated,
        )
        corrections += count
    distributions = authority.get("distributions")
    if isinstance(distributions, Mapping):
        for distribution in distributions.values():
            if not isinstance(distribution, Mapping):
                continue
            counts = distribution.get("counts")
            if not isinstance(counts, Mapping):
                continue
            for label, expected in counts.items():
                label_pattern = re.escape(str(label))
                if str(label) == "모집중":
                    label_pattern = r"모집\s*중"
                pattern = re.compile(
                    rf"(?P<prefix>{label_pattern}(?:은|는|이|가)?\s*)"
                    r"(?P<count>[\d,]+)(?P<suffix>건)"
                )
                updated, count = pattern.subn(
                    lambda match, value=int(expected): (
                        f"{match.group('prefix')}{value:,}{match.group('suffix')}"
                    ),
                    updated,
                )
                corrections += count
        updated, count = _replace_distribution_prose(updated, distributions)
        corrections += count
    return updated, corrections


def _replace_distribution_prose(
    text: str,
    distributions: Mapping[str, Any],
) -> tuple[str, int]:
    summaries: dict[str, str] = {}
    display_labels = {"모집중": "모집 중", "미확인": "미확인"}
    names = {"status": "상태", "phase": "단계", "sponsor": "스폰서"}
    for name, distribution in distributions.items():
        if not isinstance(distribution, Mapping):
            continue
        counts = distribution.get("counts")
        population = distribution.get("population")
        if not isinstance(counts, Mapping) or not isinstance(population, int):
            continue
        rendered = " · ".join(
            f"{display_labels.get(str(label), str(label))} {int(value):,}건"
            for label, value in counts.items()
        )
        summaries[str(name)] = (
            f"직접 관련 {population:,}건의 {names[str(name)]} 분포는 {rendered}입니다."
        )

    replacement_count = 0

    sponsor_pattern = re.compile(
        r"직접\s*관련\s*\d[\d,]*건의\s*스폰서\s*분포는[^\n]*?입니다\."
    )
    sponsor_replaced = 0
    if sponsor_summary := summaries.get("sponsor"):
        text, sponsor_replaced = sponsor_pattern.subn(sponsor_summary, text)
        replacement_count += sponsor_replaced

    sentence_pattern = re.compile(r"[^\n]+?(?:[.!?](?=\s|$)|$)")

    def replacement(match: re.Match[str]) -> str:
        nonlocal replacement_count
        sentence = match.group(0)
        count_mentions = len(re.findall(r"\d[\d,]*건", sentence))
        kind = None
        status_terms = sum(
            term in sentence
            for term in ("완료", "모집 중", "모집 전", "활성", "중단", "미확인")
        )
        if "status" in summaries and count_mentions >= 2 and status_terms >= 2:
            kind = "status"
        elif (
            "phase" in summaries
            and count_mentions >= 2
            and ("단계" in sentence or len(re.findall(r"\d상", sentence)) >= 2)
        ):
            kind = "phase"
        elif (
            not sponsor_replaced
            and "sponsor" in summaries
            and count_mentions >= 2
            and "스폰서" in sentence
        ):
            kind = "sponsor"
        if kind is None:
            return sentence
        replacement_count += 1
        prefix = " " if sentence.startswith(" ") else ""
        return prefix + summaries[kind]

    return sentence_pattern.sub(replacement, text), replacement_count


def align_text_to_table_authority(
    text: str,
    table_texts: Sequence[str],
) -> tuple[str, dict[str, Any]]:
    """Align one releasable section to deterministic table populations."""

    authority = _table_authority(table_texts)
    aligned, corrections = _align_table_authority(text, authority)
    mismatch_count = int(authority.get("distribution_sum_mismatch", 0))
    return aligned, {
        "authority": "structured_tables",
        "direct_related_count": authority.get("direct_related"),
        "product_patent_count": authority.get("product_patents"),
        "correction_count": corrections,
        "distribution_sum_mismatch_count": mismatch_count,
        "sigma_checked_count": int(authority.get("sigma_checked_count", 0)),
        "distribution_checks": deepcopy(authority.get("distributions", {})),
        "unresolved_count": mismatch_count,
    }


def normalize_structured_answer_trace(
    text: str,
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply table-authoritative parity and remove structured tables from section text."""

    from jw_chat_agent_poc.service.v4.insight_claim_verifier import (
        normalize_final_surface_text,
    )

    projected = deepcopy(dict(trace))
    answer_sections = _answer_sections_payload(projected)
    if answer_sections is None:
        return projected
    table_texts: list[str] = [text]
    semantic_realization = projected.get("semantic_realization")
    if isinstance(semantic_realization, Mapping):
        semantic_text = semantic_realization.get("text")
        if isinstance(semantic_text, str) and semantic_text.strip():
            table_texts.insert(0, semantic_text)
    table_texts.extend(
        str(value)
        for value in answer_sections["content"].values()
        if str(value).strip()
    )
    authority = _table_authority(table_texts)
    corrections = 0
    content_counters: dict[str, dict[str, int]] = {}
    paragraph_counters: dict[str, dict[str, int]] = {}
    content_template_seen: set[str] = set()
    for section_id, section_text in tuple(answer_sections["content"].items()):
        content_sentence_seen: set[str] = set()
        aligned, count = _align_table_authority(str(section_text), authority)
        normalized, counts = normalize_final_surface_text(
            aligned,
            template_seen=content_template_seen,
            sentence_seen=content_sentence_seen,
        )
        content_counters[section_id] = {key: int(value) for key, value in counts.items()}
        answer_sections["content"][section_id] = strip_structured_markdown_tables(normalized)
        corrections += count
    paragraph_template_seen: set[str] = set()
    for section_id, items in answer_sections.get("paragraphs", {}).items():
        section_counts: dict[str, int] = {}
        paragraph_sentence_seen: set[str] = set()
        checked_paragraphs = 0
        for paragraph in items:
            aligned, count = _align_table_authority(
                str(paragraph.get("text") or ""), authority
            )
            normalized, counts = normalize_final_surface_text(
                aligned,
                template_seen=paragraph_template_seen,
                sentence_seen=paragraph_sentence_seen,
            )
            for key, value in counts.items():
                section_counts[key] = section_counts.get(key, 0) + int(value)
            paragraph["text"] = strip_structured_markdown_tables(normalized)
            corrections += count
            checked_paragraphs += 1
        section_counts["final_form_checked_paragraph_count"] = checked_paragraphs
        paragraph_counters[section_id] = section_counts
    answer_sections["paragraphs"] = {
        section_id: [item for item in items if str(item.get("text") or "").strip()]
        for section_id, items in answer_sections.get("paragraphs", {}).items()
    }
    projected["answer_sections"] = answer_sections
    surface_counters: dict[str, int] = {}
    for section_id in answer_sections["content"]:
        actual_counts = paragraph_counters.get(section_id, content_counters.get(section_id, {}))
        for key, value in actual_counts.items():
            surface_counters[key] = surface_counters.get(key, 0) + int(value)
    mismatch_count = int(authority.get("distribution_sum_mismatch", 0))
    current_parity = {
        "authority": "structured_tables",
        "direct_related_count": authority.get("direct_related"),
        "product_patent_count": authority.get("product_patents"),
        "correction_count": corrections,
        "distribution_sum_mismatch_count": mismatch_count,
        "sigma_checked_count": int(authority.get("sigma_checked_count", 0)),
        "distribution_checks": deepcopy(authority.get("distributions", {})),
        "unresolved_count": mismatch_count,
    }
    prior_parity = projected.get("table_body_parity")
    if (
        isinstance(prior_parity, Mapping)
        and int(prior_parity.get("sigma_checked_count") or 0)
        > int(current_parity["sigma_checked_count"])
        and int(prior_parity.get("unresolved_count") or 0) == 0
    ):
        current_parity = deepcopy(dict(prior_parity))
        current_parity["correction_count"] = max(
            int(current_parity.get("correction_count") or 0), corrections
        )
    projected["table_body_parity"] = current_parity
    projected["final_sse_surface_counters"] = surface_counters
    return projected


def _answer_sections_payload(
    trace: Mapping[str, Any],
) -> dict[str, Any] | None:
    payload = trace.get("answer_sections")
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema") != "jw.answer-sections.v1":
        return None
    sections = payload.get("sections")
    content = payload.get("content")
    paragraphs = payload.get("paragraphs", {})
    evidence_catalog = payload.get("evidence_catalog", {})
    if not isinstance(sections, Sequence) or not isinstance(content, Mapping):
        return None
    if not isinstance(paragraphs, Mapping):
        paragraphs = {}
    if not isinstance(evidence_catalog, Mapping):
        evidence_catalog = {}
    normalized_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            return None
        section_id = str(section.get("id") or "")
        if section_id not in {"insight", "facts"} or section_id not in content:
            return None
        normalized_sections.append(dict(section))
    if {str(section["id"]) for section in normalized_sections} != {
        "insight",
        "facts",
    }:
        return None
    normalized_paragraphs: dict[str, list[dict[str, Any]]] = {}
    for section_id, items in paragraphs.items():
        if section_id not in {"insight", "facts"} or not isinstance(items, Sequence):
            continue
        normalized_items: list[dict[str, Any]] = []
        label_counts = {"가설": 0, "해석": 0}
        for item in items:
            if not isinstance(item, Mapping) or not str(item.get("text") or "").strip():
                continue
            text = str(item["text"]).strip()
            label_match = _FINAL_LABEL_RE.match(text)
            if label_match:
                label = label_match.group(1)
                label_counts[label] += 1
                text = (
                    f"**[{label} {label_counts[label]}]** "
                    f"{text[label_match.end():].lstrip()}"
                ).rstrip()
            evidence = item.get("evidence", ())
            normalized_evidence = [
                {
                    "evidence_id": str(entry["evidence_id"]),
                    "label": str(entry.get("label") or "출처"),
                }
                for entry in evidence
                if isinstance(entry, Mapping) and str(entry.get("evidence_id") or "")
            ] if isinstance(evidence, Sequence) else []
            normalized_evidence = list(
                {
                    entry["evidence_id"]: entry
                    for entry in normalized_evidence
                }.values()
            )
            normalized_item: dict[str, Any] = {
                "text": text,
                "evidence": normalized_evidence,
                "unsourced": bool(item.get("unsourced", not normalized_evidence)),
                "paragraph_start": bool(
                    label_match
                    or item.get("paragraph_start", not normalized_items)
                ),
            }
            evidence_group = _normalize_evidence_group(item.get("evidence_group"))
            if evidence_group is not None:
                normalized_item["evidence_group"] = evidence_group
            normalized_items.append(normalized_item)
        if normalized_items:
            normalized_paragraphs[str(section_id)] = normalized_items
    return {
        "schema": "jw.answer-sections.v1",
        "sections": normalized_sections,
        "content": dict(content),
        "evidence_catalog": {
            str(evidence_id): dict(record)
            for evidence_id, record in evidence_catalog.items()
            if str(evidence_id) and isinstance(record, Mapping)
        },
        "paragraphs": normalized_paragraphs,
    }


def _normalize_evidence_group(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("schema") != "jw.evidence-group.v1":
        return None
    group_id = str(value.get("group_id") or "").strip()
    primary = value.get("primary")
    members = value.get("members")
    breakdown = value.get("source_breakdown")
    if (
        not group_id
        or not isinstance(primary, Mapping)
        or not isinstance(members, Sequence)
        or not isinstance(breakdown, Sequence)
    ):
        return None

    normalized_members = [
        {
            "evidence_id": str(member["evidence_id"]),
            "label": str(member.get("label") or "출처"),
            "source_key": str(member.get("source_key") or ""),
            "source_label": str(member.get("source_label") or "출처"),
        }
        for member in members
        if isinstance(member, Mapping) and str(member.get("evidence_id") or "")
    ]
    normalized_members = list(
        {member["evidence_id"]: member for member in normalized_members}.values()
    )
    primary_id = str(primary.get("evidence_id") or "")
    normalized_primary = next(
        (member for member in normalized_members if member["evidence_id"] == primary_id),
        None,
    )
    if normalized_primary is None or len(normalized_members) < 2:
        return None
    normalized_breakdown: list[dict[str, Any]] = []
    for entry in breakdown:
        if not isinstance(entry, Mapping):
            continue
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        normalized_breakdown.append(
            {
                "source_key": str(entry.get("source_key") or ""),
                "source_label": str(entry.get("source_label") or "출처"),
                "count": count,
            }
        )
    if sum(entry["count"] for entry in normalized_breakdown) != len(normalized_members):
        return None
    return {
        "schema": "jw.evidence-group.v1",
        "group_id": group_id,
        "primary": normalized_primary,
        "members": normalized_members,
        "source_breakdown": normalized_breakdown,
    }


def iter_legacy_final_answer_events(
    *,
    conversation_id: str | None,
    source_labels: Sequence[str],
    file_sources: Sequence[Mapping[str, Any]],
    text: str,
    charts: Sequence[Mapping[str, Any]],
    timing: Mapping[str, Any],
    trace: Mapping[str, Any],
    streamed_prefix: str = "",
    streamed_section_ids: Sequence[str] = (),
) -> Iterator[str]:
    del streamed_section_ids
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
    remaining_text = _remaining_after_streamed_prefix(text, streamed_prefix)
    if remaining_text:
        yield from iter_markdown_sse_events(remaining_text)
    if charts:
        yield legacy_sse_json_event("charts", charts)
    yield legacy_sse_json_event("timing", timing)
    yield legacy_sse_json_event("trace", trace)
    yield "event: done\ndata: ok\n\n"


def _remaining_after_streamed_prefix(text: str, streamed_prefix: str) -> str:
    if not streamed_prefix:
        return text
    prefix_start = text.find(streamed_prefix)
    if prefix_start >= 0:
        remaining = (
            text[:prefix_start] + text[prefix_start + len(streamed_prefix) :]
        ).strip()
        return f"\n\n{remaining}" if remaining else ""

    streamed_headings = {
        match.group(1).strip() for match in _SECTION_HEADING_RE.finditer(streamed_prefix)
    }
    if not streamed_headings:
        return ""
    matches = list(_SECTION_HEADING_RE.finditer(text))
    if not matches:
        return ""
    output = [text[: matches[0].start()]]
    for index, match in enumerate(matches):
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : section_end]
        if match.group(1).strip() not in streamed_headings:
            output.append(section)
            continue
        notice_start = section.find("\n\n> ")
        if notice_start >= 0:
            output.append(section[notice_start:].lstrip())
    remaining = "\n\n".join(part.strip() for part in output if part.strip()).strip()
    return f"\n\n{remaining}" if remaining else ""


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
        timing: Mapping[str, Any],
        trace: Mapping[str, Any],
        streamed_prefix: str = "",
        streamed_section_ids: Sequence[str] = (),
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
