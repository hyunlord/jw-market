from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract
from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.orchestrator.market_answer_contract import enforce_market_answer_contract
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.service.answer_safety import (
    cleanup_markdown_answer,
    ensure_cross_file_comparison_judgment,
    ensure_deep_research_structure,
    ensure_file_absence_statement,
    ensure_file_overview_evidence_coverage,
    ensure_file_page_evidence,
    ensure_hira_patient_summary,
    ensure_multi_file_evidence_coverage,
    replace_internal_fact_dump,
)
from jw_chat_agent_poc.service.genos_client import (
    append_blocked_metric_notices_from_markdown_response,
    append_deferred_prescription_notice,
    append_source_basis_notice,
)
from jw_chat_agent_poc.service.markdown_cleanup import scrub_internal_terminology


ANSWER_PIPELINE_ENV = "JW_CHAT_ANSWER_PIPELINE_ENABLED"
PIPELINE_DEDUP_ENV = "JW_CHAT_PIPELINE_DEDUP_ENABLED"

PRE_CHART_STAGE_NAMES = (
    "cleanup",
    "answer_contract_first",
    "claim_policy_repeat",
    "answer_contract_second",
    "empty_file_context_fallback",
    "file_page_evidence",
    "file_overview_evidence",
    "cross_file_comparison",
    "multi_file_evidence",
    "file_context_source",
    "blocked_metric_notices",
    "common_unavailable_response",
    "source_basis_notice",
    "replace_internal_fact_dump",
    "requested_source_trap",
    "file_absence_statement",
    "hira_patient_summary",
    "claim_policy_post",
    "natural_fact_lead",
    "relational_claim_pre_market",
    "market_answer_contract",
    "file_postprocess_isolation",
    "deep_claim_policy",
    "deep_research_structure",
    "relational_claim_final",
    "general_view_contract",
    "evidence_binding",
)

POST_CHART_STAGE_NAMES = (
    "deferred_prescription_notice",
    "internal_terminology_scrub",
    "verified_progress_strip",
)


@dataclass(frozen=True)
class AnswerPipelineStage:
    name: str
    transform: Callable[[str], str]


@dataclass(frozen=True)
class AnswerPipelineContext:
    question: str
    result: dict[str, Any]
    markdown_response: Any
    fact_md: str
    policy_fact_md: str
    file_context_fact: str
    deep_mode: bool
    market_contract_allowed: bool
    general_contracts_allowed: bool
    external_tool_agent_result: bool
    empty_file_answer: Callable[[str], bool]
    file_context_fallback: Callable[[str], str]
    append_file_context_source: Callable[[str, str, str], str]
    record_source_notice: Callable[[bool], None]
    relational_claim_gate: Callable[[str], str]
    natural_fact_lead: Callable[[str], str]
    file_postprocess_isolation: Callable[[str], str]
    evidence_binding_gate: Callable[[str], str]
    strip_verified_progress: Callable[[str], str]


def answer_pipeline_enabled() -> bool:
    return os.getenv(ANSWER_PIPELINE_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def pipeline_dedup_enabled() -> bool:
    return os.getenv(PIPELINE_DEDUP_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def run_answer_pipeline(answer: str, stages: Sequence[AnswerPipelineStage]) -> str:
    for pipeline_stage in stages:
        answer = pipeline_stage.transform(answer)
    return answer


def ordered_stages(
    transforms: dict[str, Callable[[str], str]],
    names: Sequence[str],
) -> tuple[AnswerPipelineStage, ...]:
    return tuple(AnswerPipelineStage(name, transforms[name]) for name in names)


def build_answer_pipeline_stages(
    context: AnswerPipelineContext,
) -> tuple[tuple[AnswerPipelineStage, ...], tuple[AnswerPipelineStage, ...]]:
    question = context.question
    result = context.result
    markdown_response = context.markdown_response
    file_context = str(result.get("file_context") or "")

    def answer_contract(answer: str) -> str:
        if (
            context.file_context_fact
            or not context.market_contract_allowed
            or not context.general_contracts_allowed
        ):
            return answer
        return enforce_answer_contract(
            question,
            answer,
            markdown_response,
            result.get("general_view_contract"),
            tool_calls=tuple(result.get("tool_calls") or ()),
        )

    def empty_file_context_fallback(answer: str) -> str:
        if not context.file_context_fact or not context.empty_file_answer(answer):
            return answer
        return apply_claim_policy(
            question,
            context.file_context_fallback(context.file_context_fact),
            context.policy_fact_md,
        )

    def source_basis_notice(answer: str) -> str:
        updated, attached = append_source_basis_notice(answer, markdown_response)
        context.record_source_notice(attached)
        return updated

    def requested_source_trap(answer: str) -> str:
        if context.file_context_fact or not context.market_contract_allowed:
            return answer
        return apply_requested_source_trap_gate(
            question,
            answer,
            identity_only=context.external_tool_agent_result or context.deep_mode,
        )

    def market_only(answer: str, transform: Callable[[str], str]) -> str:
        if context.file_context_fact or not context.market_contract_allowed or context.deep_mode:
            return answer
        return transform(answer)

    def claim_policy(answer: str) -> str:
        return apply_claim_policy(question, answer, context.policy_fact_md)

    answer_contract_transform = answer_contract
    claim_policy_transform = claim_policy
    if pipeline_dedup_enabled():
        from jw_chat_agent_poc.service.pipeline_dedup import memoize_exact_input

        answer_contract_transform = memoize_exact_input(answer_contract_transform)
        claim_policy_transform = memoize_exact_input(claim_policy_transform)

    transforms: dict[str, Callable[[str], str]] = {
        "cleanup": cleanup_markdown_answer,
        "answer_contract_first": answer_contract_transform,
        "claim_policy_repeat": claim_policy_transform,
        "answer_contract_second": answer_contract_transform,
        "empty_file_context_fallback": empty_file_context_fallback,
        "file_page_evidence": lambda answer: ensure_file_page_evidence(question, answer, file_context),
        "file_overview_evidence": lambda answer: ensure_file_overview_evidence_coverage(question, answer, file_context),
        "cross_file_comparison": lambda answer: ensure_cross_file_comparison_judgment(question, answer, file_context),
        "multi_file_evidence": lambda answer: ensure_multi_file_evidence_coverage(question, answer, file_context),
        "file_context_source": lambda answer: context.append_file_context_source(
            answer,
            context.fact_md,
            context.file_context_fact,
        ),
        "blocked_metric_notices": lambda answer: append_blocked_metric_notices_from_markdown_response(
            answer,
            markdown_response,
        ),
        "common_unavailable_response": lambda answer: apply_common_unavailable_response(
            question,
            answer,
            markdown_response,
            tool_calls=result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else (),
            source_scope=str(result.get("context_scope") or "MARKET"),
            connected_source_mode=context.external_tool_agent_result or context.deep_mode,
        ),
        "source_basis_notice": source_basis_notice,
        "replace_internal_fact_dump": lambda answer: replace_internal_fact_dump(
            question,
            answer,
            markdown_response,
        ),
        "requested_source_trap": requested_source_trap,
        "file_absence_statement": lambda answer: ensure_file_absence_statement(question, answer, file_context),
        "hira_patient_summary": lambda answer: ensure_hira_patient_summary(question, answer, context.fact_md),
        "claim_policy_post": lambda answer: (
            answer if context.deep_mode else claim_policy_transform(answer)
        ),
        "natural_fact_lead": context.natural_fact_lead,
        "relational_claim_pre_market": lambda answer: market_only(answer, context.relational_claim_gate),
        "market_answer_contract": lambda answer: market_only(
            answer,
            lambda current: enforce_market_answer_contract(
                question,
                current,
                result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else (),
            ),
        ),
        "file_postprocess_isolation": context.file_postprocess_isolation,
        "deep_claim_policy": lambda answer: (
            claim_policy_transform(answer) if context.deep_mode else answer
        ),
        "deep_research_structure": lambda answer: (
            ensure_deep_research_structure(answer) if context.deep_mode else answer
        ),
        "relational_claim_final": context.relational_claim_gate,
        "general_view_contract": lambda answer: (
            enforce_general_view_contract(answer, result.get("general_view_contract"))
            if not context.deep_mode and not context.file_context_fact and context.market_contract_allowed
            else answer
        ),
        "evidence_binding": lambda answer: (
            context.evidence_binding_gate(answer)
            if not context.file_context_fact and context.market_contract_allowed
            else answer
        ),
        "deferred_prescription_notice": lambda answer: append_deferred_prescription_notice(answer, result),
        "internal_terminology_scrub": scrub_internal_terminology,
        "verified_progress_strip": context.strip_verified_progress,
    }
    return (
        ordered_stages(transforms, PRE_CHART_STAGE_NAMES),
        ordered_stages(transforms, POST_CHART_STAGE_NAMES),
    )


def run_selected_answer_pipeline(
    answer: str,
    stages: Sequence[AnswerPipelineStage],
    *,
    legacy: Callable[[str], str],
) -> str:
    if not answer_pipeline_enabled():
        return legacy(answer)
    return run_answer_pipeline(answer, stages)
