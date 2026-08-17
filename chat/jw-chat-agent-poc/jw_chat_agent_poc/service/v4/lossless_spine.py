from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
import os
import re
from typing import Any, Literal

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.evidence_sets import build_evidence_sets
from jw_chat_agent_poc.service.v4.gates import is_public_source_url
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CompositionResult,
    DeterministicRender,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.source_labels import normalize_public_source_surface
from jw_chat_agent_poc.service.v4.surface_notices import append_automatic_fact_notices
from jw_chat_agent_poc.service.v4.synthesis_policy import limit_evidence_sets_for_render


LosslessMode = Literal["shadow", "inject"]
RequestedFieldsMode = Literal["shadow", "inject"]
RequestSatisfactionMode = Literal["shadow", "inject"]

_SECTION_RE = re.compile(r"(?m)^#{1,6}\s+([^\n]+?)\s*$")
_CORE_HEADINGS = {"핵심 답", "핵심 요약"}
_CONTEXT_HEADINGS = {"근거와 맥락", "근거"}
_INSIGHT_HEADINGS = {"종합 인사이트", "인사이트"}
_LIMIT_HEADINGS = {"해석 상한", "해석상 주의점", "미확인 요소", "한계"}
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_UNPROVIDED_CELL = "원천 미제공"
_COVERAGE_RE = re.compile(
    r"원천 검색 (?P<total>[^·]+?)건\s*·\s*수신 (?P<received>[\d,]+)건\s*·\s*"
    r"중복 제거 후 (?P<unique>[\d,]+)건\s*·\s*상세 표시 (?P<shown>[\d,]+)건"
)
_AXIS_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    ("sales", ("매출", "실적"), ("market:",), "mart"),
    ("market_share", ("점유율", "시장점유"), ("market:",), "mart"),
    ("patient_statistics", ("환자수", "환자 수", "통계"), ("hira-statistics:",), "hira"),
    ("clinical", ("임상", "nct"), ("clinical:",), "clinicaltrials"),
    ("patent", ("특허",), ("patent:",), "patent"),
    ("reimbursement", ("급여기준", "급여 기준", "고시"), ("policy:",), "hira"),
    ("approval", ("허가", "품목"), ("nedrug:", "openfda:"), "nedrug"),
    ("document", ("문서", "파일", "첨부"), ("document:",), "document"),
)
_SOURCE_LABELS = {
    "mart": "내부 데이터마트",
    "hira": "건강보험심사평가원",
    "clinicaltrials": "ClinicalTrials.gov",
    "patent": "특허 자료",
    "nedrug": "식품의약품안전처 의약품 정보",
    "openfda": "미국 의약품 공개 정보",
    "web": "공개 웹 자료",
    "document": "업로드 문서",
}
_AXIS_LABELS = {
    "sales": "매출",
    "market_share": "점유율",
    "patient_statistics": "환자수",
    "clinical": "임상 현황",
    "patent": "특허 현황",
    "reimbursement": "급여기준",
    "approval": "허가 정보",
    "document": "문서 내용",
}
_PRIMARY_TABLE_ROW_LIMIT = 15
_HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD = 3


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
    source_render_limit: int | None = None,
) -> tuple[tuple[EvidenceSet, ...], DeterministicRender]:
    evidence_sets = build_evidence_sets(plan, results, observed_on=observed_on)
    render_sets = evidence_sets
    limit_trace: dict[str, Any] = {"applied": False, "sources": {}}
    if source_render_limit is not None:
        render_sets, limit_trace = limit_evidence_sets_for_render(
            evidence_sets,
            per_source_limit=source_render_limit,
        )
    rendered = render_deterministic_facts(
        plan,
        render_sets,
        observed_on=observed_on,
    )
    rendered = rendered.model_copy(
        update={
            "selection_rule": limit_trace.get(
                "selection_rule",
                "leading_records_in_upstream_order",
            ),
            "selection_is_ranked": limit_trace.get("selection_is_ranked", False),
        }
    )
    if limit_trace["applied"]:
        # Say how the shown ones were chosen. They are the leading records in the
        # order upstream returned them, with no relevance ranking, and a bare
        # "40/1004 표시" invites the reader to assume the 40 are the best 40.
        ranked = limit_trace.get("selection_is_ranked", False)
        basis = "관련도 순" if ranked else "상류 반환 순서의 앞 항목(임의 선택)"
        notices = [
            f"{source}: {counts['shown']}/{counts['total']} 표시({basis}), "
            "나머지는 조회 상세에 보존"
            for source, counts in limit_trace["sources"].items()
        ]
        existing = str(rendered.request_notice or "").strip()
        rendered = rendered.model_copy(
            update={
                "request_notice": " · ".join(filter(None, (existing, *notices)))
            }
        )
    return evidence_sets, rendered


def compose_lossless_answer(
    rendered: DeterministicRender,
    commentary: str,
    *,
    synthesis_trace: Mapping[str, Any],
    mode: LosslessMode,
    requested_fields_mode: RequestedFieldsMode = "shadow",
    request_satisfaction_mode: RequestSatisfactionMode = "inject",
    question: str = "",
) -> CompositionResult:
    commentary = _normalize_known_bold_headings(commentary)
    commentary = _drop_empty_bold_headings(commentary)
    if mode == "inject":
        commentary, model_source_lines_ignored = _strip_model_source_sections(commentary)
    else:
        model_source_lines_ignored = 0
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
        "model_source_lines_ignored": model_source_lines_ignored,
        "record_surface_rate": rendered.record_surface_rate,
        "required_field_surface_rate": rendered.required_field_surface_rate,
        "fallback_detail_retention_rate": retention,
        "records_received": rendered.coverage.records_received,
        "records_unique": rendered.coverage.records_unique,
        "records_rendered": rendered.coverage.records_rendered,
        "rendered_table_rows": rendered.coverage.records_rendered,
        "lossless_records_rendered": rendered.coverage.records_rendered,
        "narrated_record_count": len(rendered.narrated_record_ids),
        "narrated_record_ids": list(rendered.narrated_record_ids),
        "unnarrated_record_count": rendered.unnarrated_record_count,
        "unnarrated_records": list(rendered.unnarrated_records),
        "narrative_identifier_parity": (
            len(rendered.narrated_record_ids) == rendered.coverage.records_rendered
        ),
        "narrative_record_accounting_complete": (
            len(rendered.narrated_record_ids) + rendered.unnarrated_record_count
            == rendered.coverage.records_rendered
        ),
        "record_field_usage": list(rendered.record_field_usage),
        "average_narrated_field_count": rendered.average_narrated_field_count,
        "loaded_field_narrative_use_rate": rendered.loaded_field_narrative_use_rate,
        "identifier_only_sentence_count": rendered.identifier_only_sentence_count,
        "selection_rule": rendered.selection_rule,
        "selection_is_ranked": rendered.selection_is_ranked,
        "answer_axis": "unknown",
        "primary_source": None,
        "axis_fallback_preserved_order": True,
        "primary_axis_absence": None,
        "secondary_records_compacted": 0,
        "primary_table_row_limit": _PRIMARY_TABLE_ROW_LIMIT,
        "primary_table_rows_hidden": 0,
        "comparison_observation_sections_removed": 0,
        "mechanical_narratives_compacted": 0,
        "homogeneous_table_promotion_threshold": _HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD,
        "homogeneous_patient_narratives_promoted": 0,
        "facts_injected_after_synthesis": False,
        "synthesis_prompt_chars": synthesis_trace.get("prompt_chars"),
        "render_nodes": [
            {
                "block_id": node.block_id,
                "record_ids": list(node.record_ids),
                "surface_fields": list(node.surface_fields),
            }
            for node in rendered.nodes
        ],
    }
    inject_facts = bool(mode == "inject" and facts)
    inject_request_notice = bool(
        rendered.request_notice and request_satisfaction_mode == "inject"
    )
    inject_source_notices = bool(mode == "inject" and rendered.source_notices)
    if not inject_facts and not inject_request_notice and not inject_source_notices:
        if mode == "inject":
            text, numeric_separator_repairs = _repair_numeric_separators(commentary)
            text, public_source_rewrites = normalize_public_source_surface(text)
            text, duplicate_leading_sentences_removed = _deduplicate_sentences(text)
            source_block = _merged_source_block(rendered, ())
            if source_block:
                text = f"{text.rstrip()}\n\n{source_block}" if text.strip() else source_block
            mutated = text.strip() != commentary.strip()
            trace["answer_mutation"] = mutated
            trace["public_source_surface"] = {"rewritten": public_source_rewrites}
            trace["numeric_separator_repairs"] = numeric_separator_repairs
            trace["duplicate_leading_sentences_removed"] = (
                duplicate_leading_sentences_removed
            )
            return CompositionResult(
                text=text.strip(),
                answer_mutated=mutated,
                fallback_detail_retention_rate=retention,
                trace=trace,
            )
        return CompositionResult(
            text=commentary,
            answer_mutated=False,
            fallback_detail_retention_rate=retention,
            trace=trace,
        )

    prefix = f"{rendered.request_notice}\n\n" if inject_request_notice else ""
    if not inject_facts and not inject_source_notices:
        text = prefix + commentary
        source_block = _merged_source_block(rendered, ())
        if source_block:
            text = f"{text.rstrip()}\n\n{source_block}" if text.strip() else source_block
    else:
        text = _assemble_injected_answer(
            rendered,
            commentary,
            fallback=fallback,
            requested_fields_mode=requested_fields_mode,
            request_notice=rendered.request_notice if inject_request_notice else None,
            question=question,
            layout_trace=trace,
        )
    text, numeric_separator_repairs = _repair_numeric_separators(text)
    text = append_automatic_fact_notices(text, _rendered_notice_sources(rendered))
    text, public_source_rewrites = normalize_public_source_surface(text)
    text, duplicate_leading_sentences_removed = _deduplicate_sentences(text)
    trace["answer_mutation"] = True
    trace["public_source_surface"] = {"rewritten": public_source_rewrites}
    trace["numeric_separator_repairs"] = numeric_separator_repairs
    narrative_character_count = _narrative_character_count(text)
    narrative_character_floor = 1500
    narrative_minimum_required = rendered.coverage.records_rendered > 0
    trace["duplicate_leading_sentences_removed"] = duplicate_leading_sentences_removed
    trace["narrative_character_count"] = narrative_character_count
    trace["narrative_character_floor"] = narrative_character_floor
    trace["narrative_character_floor_met"] = (
        narrative_character_count >= narrative_character_floor
    )
    trace["narrative_minimum_required"] = narrative_minimum_required
    trace["narrative_shortfall_reason"] = (
        f"validated prose below {narrative_character_floor} characters"
        if narrative_minimum_required
        and narrative_character_count < narrative_character_floor
        else None
    )
    trace["requested_fields_injected"] = bool(
        inject_facts
        and requested_fields_mode == "inject"
        and requested_fields_observed
    )
    trace["request_notice_injected"] = inject_request_notice
    trace["source_notices_injected"] = inject_source_notices
    trace["facts_injected_after_synthesis"] = inject_facts
    return CompositionResult(
        text=text.strip(),
        answer_mutated=True,
        fallback_detail_retention_rate=retention,
        trace=trace,
    )


def _rendered_notice_sources(rendered: DeterministicRender) -> tuple[str, ...]:
    source_by_prefix = {
        "hira-statistics": "hira",
        "openfda": "openfda",
        "clinical": "clinicaltrials",
        "patent": "patent",
    }
    return tuple(
        dict.fromkeys(
            source
            for node in rendered.nodes
            if node.record_ids
            for prefix, source in source_by_prefix.items()
            if node.block_id.startswith(f"{prefix}:")
        )
    )


def _assemble_injected_answer(
    rendered: DeterministicRender,
    commentary: str,
    *,
    fallback: bool,
    requested_fields_mode: RequestedFieldsMode,
    request_notice: str | None,
    question: str,
    layout_trace: dict[str, Any],
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
        fallback_text="## 핵심 답\n해설은 생성하지 못했고 조회 결과만 표시합니다.",
    )

    axis, primary_prefixes, primary_source = _question_axis(question)
    layout_trace["answer_axis"] = axis
    layout_trace["primary_source"] = primary_source
    layout_trace["axis_fallback_preserved_order"] = axis == "unknown"

    coverage_nodes: list[RenderNode] = []
    fact_narratives: list[str] = []
    fact_nodes: list[tuple[RenderNode, str]] = []
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
        if node.block_id == "market:records":
            visible_text, node_omitted_columns = node.text.strip(), ()
        else:
            visible_text, node_omitted_columns = _omit_fully_unprovided_columns(
                node.text.strip()
            )
        omitted_columns.extend(node_omitted_columns)
        if not visible_text:
            continue
        if node.block_id.endswith(":coverage"):
            coverage_nodes.append(node.model_copy(update={"text": visible_text}))
        elif node.block_id.startswith("narrative:"):
            fact_narratives.append(visible_text)
        elif node.block_id.endswith(":limits"):
            fact_limits.append(visible_text)
        else:
            fact_nodes.append((node, visible_text))

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

    if axis == "unknown":
        blocks = [
            *fact_narratives,
            *commentary_blocks,
            *(_consolidated_coverage(coverage_nodes)),
            *(text for _, text in fact_nodes),
            *fact_limits,
            *(_render_sections(limits)),
        ]
    else:
        primary_nodes = [
            (node, text)
            for node, text in fact_nodes
            if node.block_id.startswith(primary_prefixes)
        ]
        commentary_blocks = _align_commentary_to_axis(
            commentary_blocks,
            axis,
            primary_nodes=primary_nodes,
        )
        comparison_blocks = [
            block for block in commentary_blocks if block.startswith("## 비교 관측\n")
        ]
        if comparison_blocks:
            commentary_blocks = [
                block for block in commentary_blocks if block not in comparison_blocks
            ]
            layout_trace["comparison_observation_sections_removed"] = len(comparison_blocks)
        secondary_nodes = [
            (node, text)
            for node, text in fact_nodes
            if not node.block_id.startswith(primary_prefixes)
        ]
        (
            commentary_blocks,
            dimension_notices,
            promoted_commentary,
        ) = _align_patient_dimension_commentary(
            commentary_blocks,
            question=question,
            axis=axis,
            primary_nodes=primary_nodes,
        )
        limits.extend(("미확인 요소", notice) for notice in dimension_notices)
        lead_blocks, deferred_commentary = _partition_lead_commentary(commentary_blocks)
        absence_block, absence_reason = _primary_absence_block(
            axis,
            primary_source,
            primary_nodes,
            rendered.source_notice_bindings,
        )
        if absence_block:
            lead_blocks.insert(0, absence_block)
            layout_trace["primary_axis_absence"] = absence_reason
        compacted, compacted_records = _compact_secondary_nodes(secondary_nodes)
        layout_trace["secondary_records_compacted"] = compacted_records
        limited_primary: list[str] = []
        hidden_primary_rows = 0
        for node, text in primary_nodes:
            limited, hidden = _limit_markdown_table_rows(
                text,
                row_limit=_PRIMARY_TABLE_ROW_LIMIT,
            )
            limited_primary.append(limited)
            hidden_primary_rows += hidden
        layout_trace["primary_table_rows_hidden"] = hidden_primary_rows
        homogeneous_patient_narratives = (
            promoted_commentary
            + _homogeneous_patient_narrative_count(
                fact_narratives,
                axis=axis,
                primary_nodes=primary_nodes,
            )
        )
        layout_trace["homogeneous_patient_narratives_promoted"] = (
            homogeneous_patient_narratives
            if homogeneous_patient_narratives >= _HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD
            else 0
        )
        layout_trace["mechanical_narratives_compacted"] = len(fact_narratives)
        core_blocks = _merge_primary_into_core(lead_blocks, limited_primary)
        blocks = [
            *core_blocks,
            *deferred_commentary,
            *compacted,
            *(_consolidated_coverage(coverage_nodes)),
            *fact_limits,
            *(_render_sections(limits)),
        ]
    source_block = _merged_source_block(rendered, source_bodies)
    if source_block:
        blocks.append(source_block)
    return "\n\n".join(block for block in blocks if block.strip()).strip()


def _question_axis(question: str) -> tuple[str, tuple[str, ...], str | None]:
    normalized = " ".join(question.casefold().split())
    for axis, tokens, prefixes, source in _AXIS_RULES:
        if any(token in normalized for token in tokens):
            return axis, prefixes, source
    return "unknown", (), None


def _align_commentary_to_axis(
    blocks: Sequence[str],
    axis: str,
    *,
    primary_nodes: Sequence[tuple[RenderNode, str]],
) -> list[str]:
    if axis != "reimbursement":
        return list(blocks)
    aligned: list[str] = []
    deferred_market: list[str] = []
    deferred_notices: list[str] = []
    allowed_notices = _notice_numbers(text for _node, text in primary_nodes)
    for block in blocks:
        if block.startswith("## 핵심 답\n"):
            body = block.removeprefix("## 핵심 답\n")
            retained: list[str] = []
            for paragraph in re.split(r"\n\s*\n", body):
                kept_sentences: list[str] = []
                for sentence in re.split(r"(?<=[.!?])\s+", paragraph.strip()):
                    if any(
                        marker in sentence
                        for marker in (
                            "[출처: 내부 데이터마트]",
                            "[출처: 시장 데이터베이스]",
                        )
                    ):
                        deferred_market.append(sentence.strip())
                        continue
                    sentence_notices = _notice_numbers((sentence,))
                    if sentence_notices and not sentence_notices <= allowed_notices:
                        deferred_notices.append(sentence.strip())
                    elif sentence.strip():
                        kept_sentences.append(sentence.strip())
                if kept_sentences:
                    retained.append(" ".join(kept_sentences))
            if retained:
                aligned.append("## 핵심 답\n" + "\n\n".join(retained))
            continue
        if not block.startswith("## 종합 인사이트\n"):
            aligned.append(block)
            continue
        body = block.removeprefix("## 종합 인사이트\n")
        if any(
            token in body
            for token in ("급여", "고시", "투여", "인정", "일반원칙", "제외기준")
        ):
            aligned.append(block)
        else:
            aligned.append("## 참고: 인접 연구\n" + body)
    if deferred_market:
        aligned.append("## 참고: 인접 연구\n" + "\n\n".join(deferred_market))
    if deferred_notices:
        aligned.append("## 참고: 관련 고시\n" + "\n\n".join(deferred_notices))
    return aligned


def _notice_numbers(values: Iterable[str]) -> set[str]:
    return {
        match.group(1)
        for value in values
        for match in re.finditer(r"(?:고시\s*)?제?(\d{4}-\d+)호", value)
    }


def _node_source(block_id: str) -> str:
    prefix = block_id.split(":", 1)[0]
    return {
        "market": "mart",
        "hira-statistics": "hira",
        "policy": "hira",
        "clinical": "clinicaltrials",
        "patent": "patent",
        "nedrug": "nedrug",
        "openfda": "openfda",
        "web": "web",
        "document": "document",
    }.get(prefix, prefix)


def _consolidated_coverage(nodes: Sequence[RenderNode]) -> list[str]:
    rows: list[str] = []
    for node in nodes:
        match = _COVERAGE_RE.search(node.text)
        if match is None:
            body = _markdown_sections(node.text)[1]
            detail = body[0][1] if body else node.text
            detail = "<br>".join(line.strip() for line in detail.splitlines() if line.strip())
            escaped_detail = detail.replace("|", "\\|")
            rows.append(
                f"| {_SOURCE_LABELS.get(_node_source(node.block_id), _node_source(node.block_id))} "
                f"| {escaped_detail} | - | - | - |"
            )
            continue
        rows.append(
            "| "
            + " | ".join(
                (
                    _SOURCE_LABELS.get(_node_source(node.block_id), _node_source(node.block_id)),
                    match.group("total").strip(),
                    match.group("received"),
                    match.group("unique"),
                    match.group("shown"),
                )
            )
            + " |"
        )
    if not rows:
        return []
    return [
        "## 조사 범위와 완전성\n"
        "| 자료원 | 원천 | 수신 | 중복 제거 후 | 표시 |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        + "\n".join(rows)
    ]


def _partition_lead_commentary(blocks: Sequence[str]) -> tuple[list[str], list[str]]:
    lead: list[str] = []
    deferred: list[str] = []
    for block in blocks:
        if block.startswith("## 핵심 답\n") and not lead:
            lead.append(block)
        elif not lead and not block.startswith("## "):
            lead.append(f"## 핵심 답\n{block}")
        else:
            deferred.append(block)
    return lead, deferred


def _merge_primary_into_core(
    lead_blocks: Sequence[str],
    primary_blocks: Sequence[str],
) -> list[str]:
    bodies: list[str] = []
    for block in lead_blocks:
        body = (
            block.removeprefix("## 핵심 답\n").strip()
            if block.startswith("## 핵심 답\n")
            else block.strip()
        )
        if body:
            bodies.append(body)
    bodies.extend(
        _demote_section_headings(block).strip()
        for block in primary_blocks
        if block.strip()
    )
    return ["## 핵심 답\n" + "\n\n".join(bodies)] if bodies else []


def _demote_section_headings(value: str) -> str:
    return re.sub(r"(?m)^##\s+", "### ", value)


def _primary_absence_block(
    axis: str,
    primary_source: str | None,
    primary_nodes: Sequence[tuple[RenderNode, str]],
    bindings: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    if primary_nodes or primary_source is None:
        return None, None
    matching = next(
        (
            binding
            for binding in bindings
            if str(binding.get("tool") or "") == primary_source
        ),
        None,
    )
    reason_code = str(matching.get("reason_code") or "") if matching else ""
    reason = _public_absence_reason(reason_code)
    suffix = f"({reason})" if reason else ""
    label = _AXIS_LABELS.get(axis, "요청하신 정보")
    return (
        f"## 핵심 답\n요청하신 {label}{_topic_particle(label)} 이번 조회에서 "
        f"확인하지 못했습니다{suffix}.",
        reason_code or None,
    )


def _topic_particle(value: str) -> str:
    if not value:
        return "는"
    last = value[-1]
    if last.isdigit():
        return "은"
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def _align_patient_dimension_commentary(
    blocks: Sequence[str],
    *,
    question: str,
    axis: str,
    primary_nodes: Sequence[tuple[RenderNode, str]],
) -> tuple[list[str], tuple[str, ...], int]:
    if axis != "patient_statistics" or not primary_nodes:
        return list(blocks), (), 0
    primary_text = "\n".join(text for _node, text in primary_nodes)
    has_age_dimension = any(token in primary_text for token in ("연령", "0~9세", "10~19세"))
    if not has_age_dimension:
        return list(blocks), (), 0

    retained: list[str] = []
    promoted = 0
    for block in blocks:
        if block.startswith(("## 근거와 맥락\n", "## 근거\n", "## 종합 인사이트\n")):
            continue
        cleaned, count = _remove_patient_restatements(block)
        promoted += count
        if cleaned:
            retained.append(cleaned)
    retained.append(
        "## 종합 인사이트\n"
        "'핵심 답'의 상병코드별 성별·연령대 수치를 종합하면 환자수는 "
        "상병코드와 연령대에 따라 다릅니다. 수신되지 않은 성별·연령대 조합은 "
        "전체 경향으로 일반화하지 않았습니다."
    )
    normalized_question = " ".join(question.casefold().split())
    has_male = "| 남 |" in primary_text
    has_female = "| 여 |" in primary_text
    notices: list[str] = []
    if "성별" in normalized_question and has_male and not has_female:
        notices.append("여성 연령대별 자료는 이번 조회에서 확인하지 못했습니다.")
    return retained, tuple(notices), promoted


_PATIENT_RESTATEMENT_RE = re.compile(
    r"(?:^|\n)\s*\d{4}년\s+\S+\s+\S+\s*·\s*\S+세\s+환자수\s+"
    r"[\d,]+명으로\s+확인되었습니다\.\s*"
    r"(?:\[출처:\s*건강보험심사평가원\])?\s*(?=\n|$)"
)


def _remove_patient_restatements(block: str) -> tuple[str, int]:
    matches = tuple(_PATIENT_RESTATEMENT_RE.finditer(block))
    if len(matches) < _HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD:
        return block, 0
    cleaned = _PATIENT_RESTATEMENT_RE.sub("\n", block)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if cleaned.startswith("## ") and "\n" not in cleaned:
        cleaned = ""
    return cleaned, len(matches)


def _homogeneous_patient_narrative_count(
    blocks: Sequence[str],
    *,
    axis: str,
    primary_nodes: Sequence[tuple[RenderNode, str]],
) -> int:
    if axis != "patient_statistics" or not any(
        "| 연령대 |" in text or "| 성별 | 연령대 |" in text
        for _node, text in primary_nodes
    ):
        return 0
    return sum(len(_PATIENT_RESTATEMENT_RE.findall(block)) for block in blocks)


def _public_absence_reason(reason_code: str) -> str | None:
    normalized = reason_code.casefold()
    if normalized in {"query_not_generated", "source_not_selected", "not_executed"}:
        return "실행 안 함"
    if "timeout" in normalized:
        return "응답 시간 초과"
    if normalized == "empty_result":
        return "성공했으나 0건"
    if any(token in normalized for token in ("quota", "limit", "rate")):
        return "쿼터·한도 소진"
    return None


def _compact_secondary_nodes(
    nodes: Sequence[tuple[RenderNode, str]],
) -> tuple[list[str], int]:
    grouped: dict[str, list[tuple[RenderNode, str]]] = {}
    for node, visible_text in nodes:
        grouped.setdefault(_secondary_group_key(node.block_id), []).append((node, visible_text))

    lines: list[str] = []
    compacted_records = 0
    for grouped_nodes in grouped.values():
        record_ids = tuple(
            dict.fromkeys(
                record_id
                for node, _visible_text in grouped_nodes
                for record_id in node.record_ids
            )
        )
        count = len(record_ids)
        if count == 0:
            continue
        first_node, first_text = grouped_nodes[0]
        heading, representative = _summary_parts(first_text)
        label = heading or _SOURCE_LABELS.get(_node_source(first_node.block_id), "참고 자료")
        representative_text = f" · 대표: {representative}" if representative else ""
        lines.append(
            f"- {label} {count}건{representative_text} · 상세 항목은 조회 상세에서 확인할 수 있습니다."
        )
        compacted_records += count
    return (["## 참고 자료\n" + "\n".join(lines)] if lines else []), compacted_records


def _secondary_group_key(block_id: str) -> str:
    parts = block_id.split(":")
    if parts[0] == "patent" and len(parts) > 1:
        return ":".join(parts[:2])
    return parts[0]


def _summary_parts(text: str) -> tuple[str, str | None]:
    _preamble, sections = _markdown_sections(text)
    heading = sections[0][0] if sections else ""
    body = sections[0][1] if sections else text
    table_rows = [
        row
        for line in body.splitlines()
        if (row := _split_markdown_row(line)) is not None
    ]
    if len(table_rows) >= 3 and table_rows[2]:
        return heading, table_rows[2][0]
    first_line = next((line.strip("- ") for line in body.splitlines() if line.strip()), "")
    return heading, first_line or None


def _limit_markdown_table_rows(text: str, *, row_limit: int) -> tuple[str, int]:
    lines = text.splitlines()
    table_indexes = [
        index for index, line in enumerate(lines) if _split_markdown_row(line) is not None
    ]
    if len(table_indexes) <= row_limit + 2:
        return text, 0
    first = table_indexes[0]
    contiguous: list[int] = []
    for index in table_indexes:
        if not contiguous or index == contiguous[-1] + 1:
            contiguous.append(index)
        elif index > first:
            break
    data_indexes = contiguous[2:]
    if len(data_indexes) <= row_limit:
        return text, 0
    hidden = len(data_indexes) - row_limit
    keep = set(contiguous[: row_limit + 2])
    output = [line for index, line in enumerate(lines) if index not in contiguous or index in keep]
    insert_at = max(keep) + 1
    output.insert(insert_at, f"전체 {len(data_indexes)}건 중 {row_limit}건 표시 · 나머지는 조회 상세에서 확인")
    return "\n".join(output), hidden


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


def _strip_model_source_sections(value: str) -> tuple[str, int]:
    preamble, sections = _markdown_sections(value)
    ignored = sum(
        1
        for heading, body in sections
        if heading == "출처"
        for line in body.splitlines()
        if line.strip()
    )
    retained = tuple((heading, body) for heading, body in sections if heading != "출처")
    blocks = ([preamble] if preamble else []) + _render_sections(retained)
    return "\n\n".join(block for block in blocks if block.strip()), ignored


_BOLD_HEADING_RE = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$")
_KNOWN_BOLD_HEADINGS = frozenset(
    {
        "핵심 답",
        "핵심 요약",
        "근거와 맥락",
        "근거",
        "종합 인사이트",
        "인사이트",
        "참고: 인접 연구",
    }
)


def _normalize_known_bold_headings(value: str) -> str:
    output: list[str] = []
    for line in value.splitlines():
        match = re.fullmatch(r"\s*\*\*([^*\n]+)\*\*\s*", line)
        heading = match.group(1).strip() if match else ""
        output.append(f"## {heading}" if heading in _KNOWN_BOLD_HEADINGS else line)
    return "\n".join(output)


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


_CITATION_ONLY_RE = re.compile(r"(?:\[[^\n\]]+\]\s*)+")


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
            sentence_without_source = re.sub(
                r"\s*\[[^\n]+\]\s*$",
                "",
                sentence,
            )
            key = re.sub(r"\s+", " ", sentence_without_source).casefold()
            if key in seen:
                removed += 1
            else:
                seen.add(key)
                parts.append(match.group(0))
            cursor = match.end()
        parts.append(line[cursor:])
        collapsed = "".join(parts).strip()
        # Dropping a duplicate sentence can leave its citation stranded on a line
        # of its own. A bare citation carries no fact, so it is noise rather than
        # evidence; the record it pointed at is still cited by the surviving copy.
        if collapsed and _CITATION_ONLY_RE.fullmatch(collapsed):
            continue
        output.append(collapsed)
    return "\n".join(output).strip(), removed


def _repair_numeric_separators(text: str) -> tuple[str, int]:
    repaired, thousands = re.subn(
        r"(?<=\d)\.\s+(?=\d{3}\s*(?:만|천|백)?\s*(?:Rx|건|명|원|억원))",
        ",",
        text,
    )
    repaired, decimals = re.subn(r"(?<=\d)\.\s+(?=\d{3}\s*%)", ".", repaired)
    return repaired, thousands + decimals


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
    del source_bodies
    lines: list[str] = []
    seen_urls: set[str] = set()
    refs_by_source: dict[str, list[Any]] = {}
    for ref in rendered.source_refs:
        if not is_public_source_url(ref.url) or ref.url in seen_urls:
            continue
        seen_urls.add(ref.url)
        source = ref.source or ref.url
        refs_by_source.setdefault(source, []).append(ref)
    for source, refs in refs_by_source.items():
        first = refs[0]
        label = _SOURCE_LABELS.get(source, first.title or "원문")
        extra = f" · 외 {len(refs) - 1}건" if len(refs) > 1 else ""
        lines.append(f"- [{label}]({first.url}){extra}")
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
