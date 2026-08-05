from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Protocol

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    V3EvidenceBundle,
    WebSourceFact,
)
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
    web_source_numeric_literals,
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
    period_span_resolution,
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


class FusionOutputTruncatedError(FusionOutputError):
    reason_code = "fusion_output_truncated"

    def __init__(self, provider: FusionProviderResult) -> None:
        super().__init__("fusion response reached the output token limit")
        self.provider = provider
        self.limitations = (
            "응답이 출력 상한에서 잘려 일부를 확인하지 못했습니다.",
        )


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
        if provider_result.finish_reason == "length":
            raise FusionOutputTruncatedError(provider_result)
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
        web_facts = tuple(
            fact for fact in cited_facts if isinstance(fact, WebSourceFact)
        )
        internal_facts = tuple(
            fact for fact in cited_facts if not isinstance(fact, WebSourceFact)
        )
        if web_facts and any(fact.url not in claim.text for fact in web_facts):
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="web_source_attribution_missing",
                    numeric_literals=observed_numbers,
                )
            )
            continue
        if web_facts and len(web_facts) == len(cited_facts) and _labels_web_as_internal(claim.text):
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="web_source_mislabeled_internal",
                    numeric_literals=observed_numbers,
                )
            )
            continue
        internal_numbers = set().union(
            *(fact_numeric_literals(fact) for fact in internal_facts)
        )
        web_numbers = {
            value
            for fact in web_facts
            for value in web_source_numeric_literals(fact)
        }
        if _web_only_number_lacks_external_label(
            claim.text,
            observed_number_spans,
            web_numbers.difference(internal_numbers),
            has_internal_facts=bool(internal_facts),
        ):
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="web_source_mislabeled_internal",
                    numeric_literals=observed_numbers,
                )
            )
            continue
        conflict_ids = {
            evidence_id
            for fact in web_facts
            for evidence_id in fact.conflicts_with_evidence_ids
        }
        if conflict_ids and not conflict_ids.issubset(claim.evidence_ids):
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="web_conflict_missing_internal_evidence",
                    numeric_literals=observed_numbers,
                )
            )
            continue
        if conflict_ids and not _has_conflict_disclosure(generated.limitations):
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason="web_conflict_not_disclosed",
                    numeric_literals=observed_numbers,
                )
            )
            continue
        allowed = internal_numbers | web_numbers
        period_resolution = period_span_resolution(claim.text, cited_facts)
        url_spans = _web_url_spans(claim.text, web_facts)
        unsupported_spans = tuple(
            (literal, start, end)
            for literal, start, end in observed_number_spans
            if not any(
                span_start <= start and end <= span_end
                for span_start, span_end in (*period_resolution.spans, *url_spans)
            )
            and canonical_numeric_literal(literal) not in allowed
        )
        unsupported = tuple(literal for literal, _, _ in unsupported_spans)
        if unsupported:
            ambiguity_reasons = tuple(
                reason
                for _, start, end in unsupported_spans
                if (reason := period_resolution.ambiguity_reason_for(start, end))
                is not None
            )
            if ambiguity_reasons and len(ambiguity_reasons) == len(unsupported_spans):
                candidate_counts = sorted(
                    {
                        reason.rsplit("_", 1)[-1]
                        for reason in ambiguity_reasons
                    },
                    key=int,
                )
                rejection_reason = (
                    "ambiguous_period_month_candidates_"
                    + "_".join(candidate_counts)
                )
            else:
                rejection_reason = "ungrounded_numeric_literal"
            rejected.append(
                RejectedFusionClaim(
                    text=claim.text,
                    evidence_ids=claim.evidence_ids,
                    reason=rejection_reason,
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


def _web_url_spans(
    text: str,
    facts: tuple[WebSourceFact, ...],
) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for fact in facts:
        start = 0
        while (found := text.find(fact.url, start)) >= 0:
            spans.append((found, found + len(fact.url)))
            start = found + len(fact.url)
    return tuple(spans)


def _labels_web_as_internal(text: str) -> bool:
    return re.search(r"내부\s*(?:데이터|마트)|마트\s*기준", text) is not None


def _web_only_number_lacks_external_label(
    text: str,
    numeric_spans: tuple[tuple[str, int, int], ...],
    web_only_numbers: set[str],
    *,
    has_internal_facts: bool,
) -> bool:
    external_markers = tuple(re.finditer(r"외부|웹|출처|보도", text))
    if not external_markers:
        return bool(web_only_numbers)
    if not has_internal_facts:
        return False
    internal_markers = tuple(
        re.finditer(r"내부\s*(?:데이터|마트)|마트\s*기준|UBIST|IQVIA", text, re.IGNORECASE)
    )
    for literal, start, _end in numeric_spans:
        if canonical_numeric_literal(literal) not in web_only_numbers:
            continue
        latest_external = max(
            (marker.start() for marker in external_markers if marker.start() < start),
            default=-1,
        )
        latest_internal = max(
            (marker.start() for marker in internal_markers if marker.start() < start),
            default=-1,
        )
        if latest_external <= latest_internal:
            return True
    return False


def _has_conflict_disclosure(limitations: tuple[str, ...]) -> bool:
    return any(
        re.search(r"차이|불일치|서로\s*다", limitation)
        and re.search(r"내부|마트|UBIST|IQVIA", limitation, re.IGNORECASE)
        and re.search(r"외부|웹|출처|보도", limitation)
        for limitation in limitations
    )


__all__ = [
    "FusionGenerationResult",
    "FusionOutputError",
    "FusionOutputTruncatedError",
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
