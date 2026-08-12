from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import os
from typing import Any, Literal

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.evidence_sets import build_evidence_sets
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CompositionResult,
    DeterministicRender,
    EvidenceSet,
)


LosslessMode = Literal["shadow", "inject"]
RequestedFieldsMode = Literal["shadow", "inject"]
RequestSatisfactionMode = Literal["shadow", "inject"]


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
        "profile": rendered.profile,
        "answer_mutation": False,
        "record_surface_rate": rendered.record_surface_rate,
        "required_field_surface_rate": rendered.required_field_surface_rate,
        "fallback_detail_retention_rate": retention,
        "records_received": rendered.coverage.records_received,
        "records_unique": rendered.coverage.records_unique,
        "records_rendered": rendered.coverage.records_rendered,
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
    if not inject_facts and not inject_request_notice:
        return CompositionResult(
            text=commentary,
            answer_mutated=False,
            fallback_detail_retention_rate=retention,
            trace=trace,
        )

    prefix = f"{rendered.request_notice}\n\n" if inject_request_notice else ""
    if not inject_facts:
        text = prefix + commentary
    elif fallback:
        text = (
            prefix
            + facts
            + "\n\n## 자동 해설\n자동 해설 생성이 완료되지 않았습니다. 위 사실면과 원문은 그대로 제공합니다."
            + _source_block(rendered)
        )
    else:
        text = (
            prefix
            + facts
            + "\n\n## 자동 해설\n"
            + commentary.strip()
            + _source_block(rendered)
        )
    trace["answer_mutation"] = True
    trace["requested_fields_injected"] = bool(
        inject_facts
        and requested_fields_mode == "inject"
        and requested_fields_observed
    )
    trace["request_notice_injected"] = inject_request_notice
    return CompositionResult(
        text=text.strip(),
        answer_mutated=True,
        fallback_detail_retention_rate=retention,
        trace=trace,
    )


def _source_block(rendered: DeterministicRender) -> str:
    if not rendered.source_refs:
        return ""
    lines = ["\n\n## 출처"]
    for ref in rendered.source_refs:
        label = ref.title or ref.url
        suffix = f" ({ref.published_at})" if ref.published_at else ""
        lines.append(f"- [{label}]({ref.url}){suffix}")
    return "\n".join(lines)


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
