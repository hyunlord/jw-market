from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Protocol

from jw_chat_agent_poc.tool_use.v3_execution_contracts import V3EvidenceBundle
from jw_chat_agent_poc.tool_use.v3_fusion_contracts import (
    FusionAnswerModel,
    FusionAudit,
    FusionClaim,
    GeneratedFusionAnswer,
    RejectedFusionClaim,
    ValidatedFusionAnswer,
)
from jw_chat_agent_poc.tool_use.v3_fusion_provider import FusionProviderResult
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import (
    build_fusion_messages,
    canonical_numeric_literal,
    fact_numeric_literals,
    fusion_citation_facts,
    message_numeric_literals,
    numeric_literal_spans,
    numeric_literals,
)
from jw_chat_agent_poc.tool_use.v3_fusion_limitations import (
    deferred_limitation,
    failure_limitation,
    failure_reason_code,
)
from jw_chat_agent_poc.tool_use.v3_fusion_semantics import (
    claim_semantic_rejection,
    ordered_unique,
    period_numeric_spans,
)


class FusionProvider(Protocol):
    def generate(self, *, messages: list[dict[str, str]]) -> FusionProviderResult: ...


@dataclass(frozen=True, slots=True)
class FusionGenerationResult:
    generated: GeneratedFusionAnswer
    validated: ValidatedFusionAnswer
    provider: FusionProviderResult


class FusionOutputError(ValueError):
    pass


class V3FusionEngine:
    def __init__(self, provider: FusionProvider) -> None:
        self._provider = provider

    def generate(
        self,
        question: str,
        bundle: V3EvidenceBundle,
    ) -> FusionGenerationResult:
        provider_result = self._provider.generate(
            messages=build_fusion_messages(question, bundle)
        )
        generated = parse_generated_answer(provider_result.content)
        return FusionGenerationResult(
            generated=generated,
            validated=validate_fusion_answer(generated, bundle),
            provider=provider_result,
        )


def parse_generated_answer(raw: str) -> GeneratedFusionAnswer:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise FusionOutputError("fusion response contains no JSON object")
    try:
        payload = json.loads(text[start : end + 1])
        return GeneratedFusionAnswer.model_validate(payload)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise FusionOutputError("fusion response violates the answer contract") from exc


def validate_fusion_answer(
    generated: GeneratedFusionAnswer,
    bundle: V3EvidenceBundle,
) -> ValidatedFusionAnswer:
    citation_facts = fusion_citation_facts(bundle.facts)
    facts = {fact.evidence_id: fact for fact in citation_facts}
    accepted: list[FusionClaim] = []
    rejected: list[RejectedFusionClaim] = []
    ungrounded: list[str] = []
    for claim in generated.claims:
        if not claim.evidence_ids:
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="missing_evidence_reference",
                )
            )
            continue
        if any(evidence_id not in facts for evidence_id in claim.evidence_ids):
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="unknown_evidence_reference",
                )
            )
            continue
        observed_numbers = numeric_literals(claim.text)
        observed_number_spans = numeric_literal_spans(claim.text)
        cited_facts = tuple(facts[evidence_id] for evidence_id in claim.evidence_ids)
        allowed = set().union(
            *(fact_numeric_literals(facts[evidence_id]) for evidence_id in claim.evidence_ids)
        )
        period_spans = period_numeric_spans(claim.text, cited_facts)
        unsupported = tuple(
            literal
            for literal, start, end in observed_number_spans
            if not any(span_start <= start and end <= span_end for span_start, span_end in period_spans)
            and canonical_numeric_literal(literal) not in allowed
        )
        if unsupported:
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="ungrounded_numeric_literal",
                    numeric_literals=unsupported,
                )
            )
            ungrounded.extend(unsupported)
            continue
        semantic_reason = claim_semantic_rejection(
            claim.text,
            cited_facts,
        )
        if semantic_reason is not None:
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason=semantic_reason,
                    numeric_literals=observed_numbers,
                )
            )
            continue
        accepted.append(FusionClaim(text=claim.text, evidence_ids=claim.evidence_ids))

    all_grounded_numbers = set().union(
        *(fact_numeric_literals(fact) for fact in citation_facts),
        *(message_numeric_literals(failure) for failure in bundle.failures),
    )
    limitations: list[str] = []
    rejected_limitations: list[str] = []
    for limitation in generated.limitations:
        limitation_period_spans = period_numeric_spans(limitation, bundle.facts)
        if any(
            not any(
                span_start <= start and end <= span_end
                for span_start, span_end in limitation_period_spans
            )
            and canonical_numeric_literal(literal) not in all_grounded_numbers
            for literal, start, end in numeric_literal_spans(limitation)
        ):
            rejected_limitations.append(limitation)
            continue
        _append_unique(limitations, limitation)

    injected_codes: list[str] = []
    for failure in bundle.failures:
        reason_code = failure_reason_code(failure)
        limitation = failure_limitation(failure, reason_code=reason_code)
        if limitation not in limitations:
            limitations.append(limitation)
            injected_codes.append(reason_code)
    for deferred in bundle.deferred:
        limitation = deferred_limitation(deferred)
        if limitation not in limitations:
            limitations.append(limitation)
            injected_codes.append("deferred_evidence")
    if rejected and not limitations:
        _append_unique(
            limitations,
            "근거와 결속되지 않은 일부 표현은 답변에서 제외했습니다.",
        )

    return ValidatedFusionAnswer(
        answer=FusionAnswerModel(claims=tuple(accepted), limitations=tuple(limitations)),
        audit=FusionAudit(
            rejected_claims=tuple(rejected),
            ungrounded_numeric_literals=tuple(ungrounded),
            rejected_limitations=tuple(rejected_limitations),
            injected_limitation_reason_codes=tuple(injected_codes),
        ),
    )


def _append_unique(items: list[str], value: str) -> None:
    normalized = value.strip()
    if normalized and normalized not in items:
        items.append(normalized)


__all__ = [
    "FusionGenerationResult",
    "FusionOutputError",
    "V3FusionEngine",
    "build_fusion_messages",
    "canonical_numeric_literal",
    "fact_numeric_literals",
    "failure_limitation",
    "failure_reason_code",
    "numeric_literals",
    "ordered_unique",
    "validate_fusion_answer",
]
