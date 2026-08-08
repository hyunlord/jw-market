from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from jw_chat_agent_poc.agent_loop.question_contracts import QuestionSpec, question_spec_for
from jw_chat_agent_poc.orchestrator.answer_claim_adapters import AnswerClaim, claims_for


class AnswerGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimPlan:
    slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerFailure:
    code: str
    subject: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerLimitation:
    code: str
    slot_id: str
    display_text: str


@dataclass(frozen=True, slots=True)
class FinalizedAnswer:
    answer: str
    claims: tuple[AnswerClaim, ...]
    limitations: tuple[AnswerLimitation, ...]
    degraded: bool
    selected_branch: str


@dataclass(frozen=True, slots=True)
class ControlLayerResult:
    answer: str
    applied: bool
    degraded: bool
    intent: str
    required_slot_coverage: str
    claim_plan_hash_input: tuple[str, ...]
    question_spec_sha256: str
    claim_plan_sha256: str
    evidence_set_sha256: str
    selected_branch: str


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _spec_hash(spec: QuestionSpec) -> str:
    return _stable_hash(
        {
            "anchor_provenance": spec.anchor_provenance,
            "forbidden_claim_types": spec.forbidden_claim_types,
            "intent": spec.intent.value,
            "operation_mode": spec.operation_mode.value,
            "optional_slots": spec.optional_slots,
            "required_slots": spec.required_slots,
        }
    )


def _passthrough_result(spec: QuestionSpec, answer: str) -> ControlLayerResult:
    empty_hash = _stable_hash(())
    return ControlLayerResult(
        answer=answer,
        applied=False,
        degraded=False,
        intent=spec.intent.value,
        required_slot_coverage="not_applied",
        claim_plan_hash_input=(),
        question_spec_sha256=_spec_hash(spec),
        claim_plan_sha256=empty_hash,
        evidence_set_sha256=empty_hash,
        selected_branch="passthrough",
    )


def _missing_limitations(spec: QuestionSpec, filled: frozenset[str]) -> tuple[AnswerLimitation, ...]:
    return tuple(
        AnswerLimitation(
            code="required_slot_unfilled",
            slot_id=slot_id,
            display_text=f"요청한 항목 '{slot_id}'은 현재 근거로 확인하지 못했습니다.",
        )
        for slot_id in spec.required_slots
        if slot_id not in filled
    )


def finalize_answer(
    question_spec: QuestionSpec,
    claim_plan: ClaimPlan,
    claims: tuple[AnswerClaim, ...],
    failures: tuple[AnswerFailure, ...],
    *,
    selected_branch: str = "v3",
    degradation_notice: str | None = None,
) -> FinalizedAnswer:
    if not question_spec.anchor_provenance:
        raise AnswerGateError("anchor_provenance_missing")

    entitled = question_spec.entitled_slots
    planned = frozenset(claim_plan.slot_ids)
    for claim in claims:
        if (
            claim.slot_id not in entitled
            or claim.slot_id not in planned
            or claim.claim_type in question_spec.forbidden_claim_types
        ):
            raise AnswerGateError(f"unentitled_claim:{claim.claim_id}")
        if not claim.evidence_ids or not claim.source:
            raise AnswerGateError(f"anchor_provenance_missing:{claim.claim_id}")

    evidence_ids = {evidence_id for claim in claims for evidence_id in claim.evidence_ids}
    if any(failure.evidence_ids and evidence_ids.intersection(failure.evidence_ids) for failure in failures):
        raise AnswerGateError("fallback_reason_inconsistent")

    filled = frozenset(claim.slot_id for claim in claims)
    limitations = _missing_limitations(question_spec, filled)
    degraded = bool(limitations or failures or selected_branch != "v3")
    if selected_branch != "v3" and not degradation_notice:
        raise AnswerGateError("silent_degradation")

    return FinalizedAnswer(
        answer="\n\n".join(claim.display_text for claim in claims),
        claims=claims,
        limitations=limitations,
        degraded=degraded,
        selected_branch=selected_branch,
    )


def _analysis_data(result: Mapping[str, Any], contract_id: str) -> Mapping[str, Any] | None:
    calls = result.get("tool_calls")
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        return None
    for call in calls:
        if not isinstance(call, Mapping) or call.get("tool") != "bq_analysis":
            continue
        data = call.get("render_data")
        if isinstance(data, Mapping) and data.get("contract_id") == contract_id:
            return data
    return None


def _controlled_result(
    spec: QuestionSpec,
    claims: tuple[AnswerClaim, ...],
) -> ControlLayerResult:
    plan = ClaimPlan(tuple(claim.slot_id for claim in claims))
    finalized = finalize_answer(spec, plan, claims, ())
    limitation_text = "\n".join(item.display_text for item in finalized.limitations)
    answer = finalized.answer
    if limitation_text:
        answer = f"{answer}\n\n### 제한\n{limitation_text}" if answer else f"### 제한\n{limitation_text}"
    evidence_ids = tuple(sorted({evidence_id for claim in claims for evidence_id in claim.evidence_ids}))
    return ControlLayerResult(
        answer=answer,
        applied=True,
        degraded=finalized.degraded,
        intent=spec.intent.value,
        required_slot_coverage=f"{len(spec.required_slots) - len(finalized.limitations)}/{len(spec.required_slots)}",
        claim_plan_hash_input=plan.slot_ids,
        question_spec_sha256=_spec_hash(spec),
        claim_plan_sha256=_stable_hash(plan.slot_ids),
        evidence_set_sha256=_stable_hash(evidence_ids),
        selected_branch="answer_projection",
    )


def apply_answer_control_layer(
    question: str,
    result: Mapping[str, Any],
    fallback_answer: str,
) -> ControlLayerResult:
    spec = question_spec_for(question)
    if result.get("context_scope") == "MIXED":
        return _passthrough_result(spec, fallback_answer)
    target_intents = frozenset({
        "MARKET_SIZE_TREND",
        "BRAND_TREND",
        "COMPETITOR_POSITION",
        "CHANNEL_SPECIALTY",
        "SALES_IMPACT",
        "MULTI_SOURCE_SNAPSHOT",
        "EXTERNAL_LOOKUP",
    })
    contract_id = {
        "MARKET_SIZE_TREND": "A1",
        "BRAND_TREND": "C1",
        "MARKET_OUTLOOK": "A2",
        "COMPETITION_CHANGE": "B1",
        "COMPETITOR_POSITION": "B2",
        "SOURCE_DIFFERENCE": "C3",
        "CHANNEL_SPECIALTY": "C2",
        "SALES_ACTIVITY_TREND": "D3",
        "SALES_IMPACT": "D2",
        "NEW_ENTRANT_THREAT": "B3",
        "MULTI_SOURCE_SNAPSHOT": "A3",
        "EXTERNAL_LOOKUP": "E1",
    }.get(spec.intent.value)
    if spec.intent.value == "SALES_ACTIVITY_TREND" and "경쟁사" not in question:
        contract_id = None
    if contract_id is None:
        return _passthrough_result(spec, fallback_answer)
    data = _analysis_data(result, contract_id)
    if data is None:
        metrics = result.get("agent_loop_metrics")
        attempted_contract = (
            metrics.get("deterministic_plan_kind")
            if isinstance(metrics, Mapping)
            else None
        )
        target_requires_control = (
            spec.intent.value in target_intents
            and (
                attempted_contract == f"BQ:{contract_id}"
                or spec.intent.value == "MULTI_SOURCE_SNAPSHOT"
            )
        )
        if result.get("answer_control_required") is True or target_requires_control:
            return _controlled_result(spec, ())
        deterministic_fallbacks: dict[str, Mapping[str, Any]] = {
            "SOURCE_DIFFERENCE": {},
            "SALES_ACTIVITY_TREND": {
                "status": "unsupported_axis",
                "insights": ["현재 CSD 도구는 경쟁사별 활동 변화를 지원하지 않습니다."],
                "evidence_refs": ["CSD.coverage"],
            },
            "NEW_ENTRANT_THREAT": {
                "launch_acceleration_status": "unsupported_missing_launch_date",
                "evidence_refs": ["market.coverage"],
            },
        }
        fallback_data = deterministic_fallbacks.get(spec.intent.value)
        if fallback_data is None:
            return _passthrough_result(spec, fallback_answer)
        fallback_claims = claims_for(spec.intent.value, fallback_data)
        return _controlled_result(spec, fallback_claims)
    return _controlled_result(spec, claims_for(spec.intent.value, data))
