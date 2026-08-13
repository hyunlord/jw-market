from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import os
import re
from typing import Any, Literal

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.evidence_sets import build_evidence_sets
from jw_chat_agent_poc.service.v4.gates import (
    contains_internal_source_reference,
    is_public_source_url,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CompositionResult,
    DeterministicRender,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.source_labels import normalize_public_source_surface


LosslessMode = Literal["shadow", "inject"]
RequestedFieldsMode = Literal["shadow", "inject"]
RequestSatisfactionMode = Literal["shadow", "inject"]

_SECTION_RE = re.compile(r"(?m)^#{1,6}\s+([^\n]+?)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")
_CORE_HEADINGS = {"핵심 답", "핵심 요약"}
_CONTEXT_HEADINGS = {"근거와 맥락", "근거"}
_INSIGHT_HEADINGS = {"종합 인사이트", "인사이트"}
_LIMIT_HEADINGS = {"해석 상한", "해석상 주의점", "미확인 요소", "한계"}
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_UNPROVIDED_CELL = "원천 미제공"


def configured_lossless_mode() -> LosslessMode:
    value = os.environ.get("CHAT_V4_LOSSLESS_SPINE_MODE", "shadow").strip().casefold()
    return "inject" if value == "inject" else "shadow"


def configured_request_satisfaction_mode() -> RequestSatisfactionMode:
    value = os.environ.get(
        "CHAT_V4_REQUEST_SATISFACTION_MODE",
        "shadow",
    ).strip().casefold()
    return "inject" if value == "inject" else "shadow"


def configured_requested_fields_mode() -> RequestedFieldsMode:
    value = os.environ.get(
        "CHAT_V4_REQUESTED_FIELDS_MODE",
        "shadow",
    ).strip().casefold()
    return "inject" if value == "inject" else "shadow"


def deterministic_fact_text(
    rendered: DeterministicRender,
    requested_fields_mode: RequestedFieldsMode,
) -> str:
    if not rendered.nodes:
        return rendered.text
    return "\n\n".join(
        node.text
        for node in rendered.nodes
        if node.text.strip()
        and (
            requested_fields_mode == "inject"
            or node.block_id != "requested-fields:absence"
        )
    )


def build_lossless_render(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    *,
    observed_on: date,
) -> tuple[tuple[EvidenceSet, ...], DeterministicRender]:
    evidence_sets = build_evidence_sets(plan, results, observed_on=observed_on)
    return evidence_sets, render_deterministic_facts(
        plan,
        evidence_sets,
        observed_on=observed_on,
    )


def compose_lossless_answer(
    rendered: DeterministicRender,
    commentary: str,
    *,
    synthesis_trace: Mapping[str, Any],
    mode: LosslessMode,
    requested_fields_mode: RequestedFieldsMode = "shadow",
    request_satisfaction_mode: RequestSatisfactionMode = "inject",
) -> CompositionResult:
    commentary = _drop_empty_bold_headings(commentary)
    fallback = bool(synthesis_trace.get("fallback_reason")) or synthesis_trace.get("status") in {
        "fallback",
        "no_usable_evidence",
    }
    retention = rendered.record_surface_rate if fallback else 1.0
    facts = deterministic_fact_text(rendered, requested_fields_mode)
    requested_fields_observed = any(
        node.block_id == "requested-fields:absence" for node in rendered.nodes
    )
    trace = {
        "mode": mode,
        "requested_fields_mode": requested_fields_mode,
        "requested_fields_observed": requested_fields_observed,
        "requested_fields_injected": False,
        "request_satisfaction_mode": request_satisfaction_mode,
        "request_notice_observed": bool(rendered.request_notice),
        "request_notice_injected": False,
        "source_notices_observed": list(rendered.source_notices),
        "source_notice_bindings": list(rendered.source_notice_bindings),
        "source_tiers": dict(rendered.source_tiers),
        "source_notices_injected": False,
        "profile": rendered.profile,
        "answer_mutation": False,
        "record_surface_rate": rendered.record_surface_rate,
        "required_field_surface_rate": rendered.required_field_surface_rate,
        "fallback_detail_retention_rate": retention,
        "records_received": rendered.coverage.records_received,
        "records_unique": rendered.coverage.records_unique,
        "records_rendered": rendered.coverage.records_rendered,
        "rendered_table_rows": rendered.coverage.records_rendered,
        "lossless_records_rendered": rendered.coverage.records_rendered,
        "render_nodes": [
            {
                "block_id": node.block_id,
                "record_ids": list(node.record_ids),
                "surface_fields": list(node.surface_fields),
            }
            for node in rendered.nodes
        ],
    }
    inject_facts = bool(
        mode == "inject"
        and rendered.profile != "market_analysis"
        and facts
    )
    inject_request_notice = bool(
        rendered.request_notice and request_satisfaction_mode == "inject"
    )
    inject_source_notices = bool(mode == "inject" and rendered.source_notices)
    if not inject_facts and not inject_request_notice and not inject_source_notices:
        return CompositionResult(
            text=commentary,
            answer_mutated=False,
            fallback_detail_retention_rate=retention,
            trace=trace,
        )

    prefix = f"{rendered.request_notice}\n\n" if inject_request_notice else ""
    if not inject_facts and not inject_source_notices:
        text = prefix + commentary
    else:
        text = _assemble_injected_answer(
            rendered,
            commentary,
            fallback=fallback,
            requested_fields_mode=requested_fields_mode,
            request_notice=rendered.request_notice if inject_request_notice else None,
        )
    text, public_source_rewrites = normalize_public_source_surface(text)
    text, duplicate_leading_sentences_removed = _deduplicate_sentences(text)
    trace["answer_mutation"] = True
    trace["public_source_surface"] = {"rewritten": public_source_rewrites}
    narrative_character_count = _narrative_character_count(text)
    narrative_minimum_required = rendered.coverage.records_rendered >= 5
    trace["duplicate_leading_sentences_removed"] = duplicate_leading_sentences_removed
    trace["narrative_character_count"] = narrative_character_count
    trace["narrative_minimum_required"] = narrative_minimum_required
    trace["narrative_shortfall_reason"] = (
        "validated prose below 1500 characters"
        if narrative_minimum_required and narrative_character_count < 1500
        else None
    )
    trace["requested_fields_injected"] = bool(
        inject_facts
        and requested_fields_mode == "inject"
        and requested_fields_observed
    )
    trace["request_notice_injected"] = inject_request_notice
    trace["source_notices_injected"] = inject_source_notices
    return CompositionResult(
        text=text.strip(),
        answer_mutated=True,
        fallback_detail_retention_rate=retention,
        trace=trace,
    )


def _assemble_injected_answer(
    rendered: DeterministicRender,
    commentary: str,
    *,
    fallback: bool,
    requested_fields_mode: RequestedFieldsMode,
    request_notice: str | None,
) -> str:
    preamble, commentary_sections = _markdown_sections(commentary)
    source_bodies = [
        body for heading, body in commentary_sections if heading == "출처" and body
    ]
    commentary_sections = [
        (heading, body)
        for heading, body in commentary_sections
        if heading not in {"출처", "자동 해설"} and body
    ]
    commentary_blocks = _question_driven_blocks(
        preamble,
        commentary_sections,
        fallback=fallback,
        fallback_text="## 핵심 답\n자동 해설 생성 미완료",
    )

    fact_coverage: list[str] = []
    fact_narratives: list[str] = []
    fact_tables: list[str] = []
    fact_limits: list[str] = []
    omitted_columns: list[str] = []
    nodes = rendered.nodes or (
        RenderNode(block_id=f"{rendered.profile}:facts", text=rendered.text),
    )
    for node in nodes:
        if (
            requested_fields_mode != "inject"
            and node.block_id == "requested-fields:absence"
        ):
            continue
        if not _has_visible_node_content(node):
            continue
        visible_text, node_omitted_columns = _omit_fully_unprovided_columns(
            node.text.strip()
        )
        omitted_columns.extend(node_omitted_columns)
        if not visible_text:
            continue
        if node.block_id.endswith(":coverage"):
            fact_coverage.append(visible_text)
        elif node.block_id.startswith("narrative:"):
            fact_narratives.append(visible_text)
        elif node.block_id.endswith(":limits"):
            fact_limits.append(visible_text)
        else:
            fact_tables.append(visible_text)

    limits: list[tuple[str, str]] = []
    if request_notice:
        limits.append(("미확인 요소", request_notice.strip()))
    if rendered.source_notices:
        limits.append(
            (
                "미확인 요소",
                "\n".join(
                    f"- [확인 한계] {notice}" for notice in rendered.source_notices
                ),
            )
        )
    if omitted_columns:
        unique_columns = tuple(dict.fromkeys(omitted_columns))
        limits.append(
            (
                "미확인 요소",
                "전 행 원천 미제공으로 생략한 열: " + ", ".join(unique_columns),
            )
        )

    blocks = [
        *commentary_blocks,
        *fact_coverage,
        *fact_narratives,
        *fact_tables,
        *fact_limits,
        *(_render_sections(limits)),
    ]
    source_block = _merged_source_block(rendered, source_bodies)
    if source_block:
        blocks.append(source_block)
    return "\n\n".join(block for block in blocks if block.strip()).strip()


def _question_driven_blocks(
    preamble: str,
    sections: Sequence[tuple[str, str]],
    *,
    fallback: bool,
    fallback_text: str,
) -> list[str]:
    if fallback:
        return [fallback_text]
    core: list[tuple[str, str]] = []
    headed: list[tuple[str, str]] = []
    unheaded: list[str] = []
    for heading, body in sections:
        if _is_source_axis_heading(heading):
            if body.strip() and body.strip() not in unheaded:
                unheaded.append(body.strip())
            continue
        if heading in _CORE_HEADINGS:
            core.append(("핵심 답", body))
        else:
            headed.append((heading, body))
    blocks: list[str] = []
    if preamble.strip():
        blocks.append(preamble.strip())
    blocks.extend(_render_sections(core))
    blocks.extend(_render_sections(headed))
    blocks.extend(unheaded)
    if not blocks and fallback_text.strip():
        blocks.append(fallback_text.strip())
    return blocks


def _is_source_axis_heading(heading: str) -> bool:
    normalized = " ".join(heading.split()).casefold()
    if not normalized.endswith((" 요약", " 보조 자료")):
        return False
    return any(
        token in normalized
        for token in (
            "fda",
            "웹 뉴스",
            "임상시험",
            "clinicaltrials",
            "건강보험심사평가원",
            "hira",
            "국내 특허",
            "특허·분쟁",
            "식품의약품안전처",
        )
    )


def _omit_fully_unprovided_columns(text: str) -> tuple[str, tuple[str, ...]]:
    lines = text.splitlines()
    output: list[str] = []
    omitted: list[str] = []
    index = 0
    while index < len(lines):
        header = _split_markdown_row(lines[index])
        separator = (
            _split_markdown_row(lines[index + 1])
            if index + 1 < len(lines)
            else None
        )
        if (
            header is None
            or separator is None
            or len(header) != len(separator)
            or not all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in separator)
        ):
            output.append(lines[index])
            index += 1
            continue

        data_rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines):
            row = _split_markdown_row(lines[cursor])
            if row is None or len(row) != len(header):
                break
            data_rows.append(row)
            cursor += 1

        omitted_indexes = tuple(
            column_index
            for column_index in range(len(header))
            if data_rows
            and all(
                row[column_index].strip() == _UNPROVIDED_CELL
                for row in data_rows
            )
        )
        if omitted_indexes:
            omitted.extend(
                header[column_index].strip()
                for column_index in omitted_indexes
            )
            retained_indexes = [
                column_index
                for column_index in range(len(header))
                if column_index not in omitted_indexes
            ]
            if retained_indexes:
                output.extend(
                    _join_markdown_row(row, retained_indexes)
                    for row in (header, separator, *data_rows)
                )
        else:
            output.extend(lines[index:cursor])
        index = cursor

    visible = "\n".join(output).strip()
    _, sections = _markdown_sections(visible)
    if sections and all(not body for _, body in sections):
        visible = ""
    return visible, tuple(omitted)


def _split_markdown_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def _join_markdown_row(row: Sequence[str], indexes: Sequence[int]) -> str:
    return "| " + " | ".join(row[index] for index in indexes) + " |"


def _markdown_sections(value: str) -> tuple[str, list[tuple[str, str]]]:
    text = value.strip()
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.end() : end].strip()))
    return preamble, sections


_BOLD_HEADING_RE = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$")


def _drop_empty_bold_headings(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        if not _BOLD_HEADING_RE.fullmatch(line):
            output.append(line)
            continue
        next_visible = next(
            (candidate for candidate in lines[index + 1 :] if candidate.strip()),
            "",
        )
        if not next_visible or _BOLD_HEADING_RE.fullmatch(next_visible) or next_visible.lstrip().startswith("#"):
            continue
        output.append(line)
    return "\n".join(output)


def _render_sections(sections: Sequence[tuple[str, str]]) -> list[str]:
    merged: list[tuple[str, list[str]]] = []
    by_heading: dict[str, list[str]] = {}
    for heading, body in sections:
        if not body.strip():
            continue
        bodies = by_heading.get(heading)
        if bodies is None:
            bodies = []
            by_heading[heading] = bodies
            merged.append((heading, bodies))
        if body.strip() not in bodies:
            bodies.append(body.strip())
    return [f"## {heading}\n" + "\n\n".join(bodies) for heading, bodies in merged]


def _deduplicate_sentences(text: str) -> tuple[str, int]:
    seen: set[str] = set()
    removed = 0
    output: list[str] = []
    sentence_re = re.compile(
        r".+?[.!?](?:\s*\[[^\n]+\])?(?=\s|$)",
    )
    for line in text.splitlines():
        if line.lstrip().startswith(("#", "|")):
            output.append(line)
            continue
        cursor = 0
        parts: list[str] = []
        for match in sentence_re.finditer(line):
            parts.append(line[cursor : match.start()])
            sentence = match.group(0).strip()
            key = re.sub(r"\s+", " ", sentence).casefold()
            if key in seen:
                removed += 1
            else:
                seen.add(key)
                parts.append(match.group(0))
            cursor = match.end()
        parts.append(line[cursor:])
        output.append("".join(parts).strip())
    return "\n".join(output).strip(), removed


def _narrative_character_count(text: str) -> int:
    prose_lines = tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", "|", "[출처:"))
        and not _TABLE_SEPARATOR_CELL_RE.fullmatch(line.strip())
    )
    return len("".join(prose_lines))


def _has_visible_node_content(node: RenderNode) -> bool:
    text = node.text.strip()
    if not text:
        return False
    _, sections = _markdown_sections(text)
    if sections and all(not body for _, body in sections):
        return False
    return not (
        not node.record_ids
        and re.search(r"(?m)^\|\s*조회 결과 없음\s*\|", text) is not None
    )


def _merged_source_block(
    rendered: DeterministicRender,
    source_bodies: Sequence[str],
) -> str:
    lines: list[str] = []
    seen_lines: set[str] = set()
    seen_urls: set[str] = set()
    for body in source_bodies:
        for raw_line in body.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if contains_internal_source_reference(line):
                continue
            urls = tuple(
                dict.fromkeys(
                    (
                        *(match.group(2).strip() for match in _MARKDOWN_LINK_RE.finditer(line)),
                        *(match.group(0).rstrip(".,;:") for match in _HTTP_URL_RE.finditer(line)),
                    )
                )
            )
            if any(not is_public_source_url(url) for url in urls):
                continue
            normalized = " ".join(line.split())
            if normalized in seen_lines:
                continue
            seen_lines.add(normalized)
            seen_urls.update(urls)
            lines.append(line)
    for ref in rendered.source_refs:
        if not is_public_source_url(ref.url) or ref.url in seen_urls:
            continue
        label = ref.title or ref.url
        suffix = f" ({ref.published_at})" if ref.published_at else ""
        lines.append(f"- [{label}]({ref.url}){suffix}")
        seen_urls.add(ref.url)
    return "" if not lines else "## 출처\n" + "\n".join(lines)


__all__ = [
    "build_evidence_sets",
    "build_lossless_render",
    "compose_lossless_answer",
    "configured_requested_fields_mode",
    "configured_lossless_mode",
    "configured_request_satisfaction_mode",
    "deterministic_fact_text",
    "render_deterministic_facts",
]
