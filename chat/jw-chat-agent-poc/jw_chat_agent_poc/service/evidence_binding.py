from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.hira_disease import hira_disease_code_for_exact_name
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact, evidence_from_calls, number_tokens
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.service.evidence_binding_rules import (
    IDENTIFIER_KEYS,
    binding_claim_number_tokens,
    claim_number_tokens,
    claim_metrics_for_token,
    entity_matches,
    expected_entity_set,
    explicit_periods,
    grade,
    has_binding_metadata,
    metric_matches,
    mismatch_reason,
    operand_binding_outcome,
    period_matches,
    present,
    question_metrics,
    requested_period_unavailable,
    unit_matches,
    without_bound_identifiers,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import explicit_disease_code

_BINDING_FAILURE_ANSWER: Final = (
    "요청 대상과 조회 근거의 대상·지표·기간 정합을 확인하지 못해 수치를 제공하지 않습니다. "
    "정확히 일치하는 권위 원천 근거를 확인한 뒤 다시 요청해 주세요."
)

_MISSING_METADATA_NOTICE: Final = (
    "근거의 기준 기간 또는 단위가 완전히 식별되지 않아 확인 가능한 값만 부분 결과로 제공합니다."
)

_SUPPLEMENTARY_NOTICE: Final = (
    "이 값은 공식 통계 원문이 아닌 공식 기관 웹의 보조 정보이며, 공식 통계로 사용해서는 안 됩니다."
)

_PARTIAL_EXCLUSION_NOTICE: Final = (
    "근거 정합을 확인하지 못한 항목은 제외하고, 확인된 항목만 제공합니다."
)

_PARTIAL_EXCLUSION_CELL: Final = "근거 불일치로 제외"


@dataclass(frozen=True, slots=True)
class BindingVerification:
    answer: str
    status: str
    disposition: str
    blocked_claim_count: int
    blocked_reasons: tuple[str, ...]
    blocked_numbers: tuple[str, ...]


def verify_claim_bindings(
    *,
    question: str,
    answer: str,
    facts: Sequence[EvidenceFact],
    expected_entities: Sequence[str] = (),
    _allow_partial_exclusion: bool = True,
) -> BindingVerification:
    expected = expected_entity_set(question, expected_entities)
    metrics = question_metrics(question)
    requested_periods = explicit_periods(question)
    if "환자수" in metrics and not expected:
        blocked_numbers = claim_number_tokens(answer)
        return BindingVerification(
            answer=_BINDING_FAILURE_ANSWER,
            status="fail",
            disposition="unavailable",
            blocked_claim_count=len(blocked_numbers),
            blocked_reasons=("MISSING_EXPECTED_ENTITY_BINDING",),
            blocked_numbers=blocked_numbers,
        )
    if not expected and not metrics:
        return BindingVerification(
            answer=answer,
            status="pass",
            disposition="answered",
            blocked_claim_count=0,
            blocked_reasons=(),
            blocked_numbers=(),
        )
    facts_by_id = {fact.fact_id: fact for fact in facts}
    claim_text = without_bound_identifiers(answer, expected)

    blocked: list[str] = []
    blocked_numbers: list[str] = []
    partial_reasons: list[str] = []
    for token in binding_claim_number_tokens(claim_text):
        candidates = tuple(
            fact
            for fact in facts
            if has_binding_metadata(fact)
            and (
                token in fact.allowed_numbers
                or token.upper() in explicit_periods(fact.period)
                or _matches_display_rounding(token, fact)
            )
        )
        if not candidates:
            token_periods = set(explicit_periods(token))
            if expected and not token_periods.intersection(requested_periods):
                blocked.append("MISSING_EVIDENCE_BINDING")
                blocked_numbers.append(token)
            continue

        claim_metrics = claim_metrics_for_token(claim_text, token) or metrics

        matching = tuple(
            fact
            for fact in candidates
            if entity_matches(fact, expected)
            and metric_matches(fact, claim_metrics)
            and period_matches(fact, requested_periods)
            and unit_matches(fact, token)
        )
        if not matching:
            if expected and all(not present(fact.entity) for fact in candidates):
                partial_reasons.append("INCOMPLETE_ENTITY_BINDING")
                continue
            if requested_periods and all(not present(fact.period) for fact in candidates):
                partial_reasons.append("INCOMPLETE_PERIOD_BINDING")
                continue
            period_compatible_except_period = tuple(
                fact
                for fact in candidates
                if entity_matches(fact, expected)
                and metric_matches(fact, claim_metrics)
                and unit_matches(fact, token)
            )
            if (
                requested_periods
                and period_compatible_except_period
                and requested_period_unavailable(answer, requested_periods)
            ):
                partial_reasons.append("REQUESTED_PERIOD_UNAVAILABLE")
                continue
            blocked.append(
                mismatch_reason(
                    candidates,
                    expected,
                    claim_metrics,
                    requested_periods=requested_periods,
                    token=token,
                )
            )
            blocked_numbers.append(token)
            continue

        grade_usable = tuple(fact for fact in matching if grade(fact) is not SourceGrade.UNVERIFIED)
        if not grade_usable:
            blocked.append("SOURCE_GRADE_MISMATCH")
            blocked_numbers.append(token)
            continue

        usable: list[EvidenceFact] = []
        operand_partial_reasons: list[str] = []
        operand_failure_reasons: list[str] = []
        for fact in grade_usable:
            outcome, reason = operand_binding_outcome(fact, facts_by_id)
            if outcome == "pass":
                usable.append(fact)
            elif outcome == "partial":
                operand_partial_reasons.append(reason)
            else:
                operand_failure_reasons.append(reason)
        if not usable:
            if operand_partial_reasons:
                partial_reasons.extend(operand_partial_reasons)
                if all(grade(fact) is SourceGrade.SUPPLEMENTARY for fact in grade_usable):
                    partial_reasons.append("SUPPLEMENTARY_SOURCE_ONLY")
                continue
            blocked.append(operand_failure_reasons[0] if operand_failure_reasons else "OPERAND_BINDING_MISMATCH")
            blocked_numbers.append(token)
            continue
        if all(
            not present(fact.period)
            or not present(fact.unit)
            or not present(fact.source_grade)
            for fact in usable
        ):
            partial_reasons.append("INCOMPLETE_BINDING_METADATA")
        if all(grade(fact) is SourceGrade.SUPPLEMENTARY for fact in usable):
            partial_reasons.append("SUPPLEMENTARY_SOURCE_ONLY")

    blocked_reasons = tuple(dict.fromkeys(blocked))
    if blocked_reasons:
        unique_blocked_numbers = tuple(dict.fromkeys(blocked_numbers))
        if _allow_partial_exclusion:
            partial_answer = _exclude_blocked_claims(answer, unique_blocked_numbers)
            remaining_claims = claim_number_tokens(
                without_bound_identifiers(partial_answer, expected)
            )
            if partial_answer != answer and remaining_claims:
                remainder = verify_claim_bindings(
                    question=question,
                    answer=partial_answer,
                    facts=facts,
                    expected_entities=expected_entities,
                    _allow_partial_exclusion=False,
                )
                if remainder.status != "fail":
                    revised = remainder.answer.rstrip()
                    if _PARTIAL_EXCLUSION_NOTICE not in revised:
                        revised = f"{revised}\n\n{_PARTIAL_EXCLUSION_NOTICE}"
                    return BindingVerification(
                        answer=revised,
                        status="partial",
                        disposition="partial",
                        blocked_claim_count=len(unique_blocked_numbers),
                        blocked_reasons=tuple(
                            dict.fromkeys((*blocked_reasons, *remainder.blocked_reasons))
                        ),
                        blocked_numbers=unique_blocked_numbers,
                    )
        return BindingVerification(
            answer=_BINDING_FAILURE_ANSWER,
            status="fail",
            disposition="unavailable",
            blocked_claim_count=len(unique_blocked_numbers),
            blocked_reasons=blocked_reasons,
            blocked_numbers=unique_blocked_numbers,
        )

    unique_partial = tuple(dict.fromkeys(partial_reasons))
    if unique_partial:
        partial_answer = answer.rstrip()
        notices = []
        if "SUPPLEMENTARY_SOURCE_ONLY" in unique_partial:
            notices.append(_SUPPLEMENTARY_NOTICE)
        if any(reason != "SUPPLEMENTARY_SOURCE_ONLY" for reason in unique_partial):
            notices.append(_MISSING_METADATA_NOTICE)
        for notice in notices:
            if notice not in partial_answer:
                partial_answer = f"{partial_answer}\n\n{notice}"
        return BindingVerification(
            answer=partial_answer,
            status="partial",
            disposition="partial",
            blocked_claim_count=0,
            blocked_reasons=unique_partial,
            blocked_numbers=(),
        )

    return BindingVerification(
        answer=answer,
        status="pass",
        disposition="answered",
        blocked_claim_count=0,
        blocked_reasons=(),
        blocked_numbers=(),
    )


def _matches_display_rounding(token: str, fact: EvidenceFact) -> bool:
    unit = _roundable_unit(token)
    if not unit or fact.unit != unit:
        return False
    claimed_text = _numeric_text(token, unit)
    claimed_places = _decimal_places(claimed_text)
    if claimed_places < 1:
        return False
    try:
        claimed = Decimal(claimed_text)
    except InvalidOperation:
        return False
    tolerance = Decimal(1).scaleb(-claimed_places) / 2
    for allowed in fact.allowed_numbers:
        if _roundable_unit(allowed) != unit:
            continue
        source_text = _numeric_text(allowed, unit)
        if _decimal_places(source_text) <= claimed_places:
            continue
        try:
            source = Decimal(source_text)
        except InvalidOperation:
            continue
        if abs(source - claimed) <= tolerance:
            return True
    return False


def _roundable_unit(value: str) -> str:
    compact = value.replace(" ", "")
    for unit in ("억원", "%p", "%", "원"):
        if compact.endswith(unit):
            return unit
    return ""


def _numeric_text(value: str, unit: str) -> str:
    return value.replace(" ", "").removesuffix(unit).replace(",", "")


def _decimal_places(value: str) -> int:
    return len(value.rsplit(".", 1)[1]) if "." in value else 0


def _exclude_blocked_claims(answer: str, blocked_numbers: Sequence[str]) -> str:
    blocked = set(blocked_numbers)
    revised_lines: list[str] = []
    for line in answer.splitlines():
        cells = _markdown_cells(line)
        if cells and not _markdown_divider(cells):
            revised_cells = [
                _PARTIAL_EXCLUSION_CELL
                if blocked.intersection(claim_number_tokens(cell))
                else cell
                for cell in cells
            ]
            revised_lines.append("| " + " | ".join(revised_cells) + " |")
            continue
        if blocked.intersection(claim_number_tokens(line)):
            kept = [
                sentence
                for sentence in re.split(r"(?<=[.!?。])\s+", line)
                if sentence
                and not blocked.intersection(claim_number_tokens(sentence))
            ]
            if kept:
                revised_lines.append(" ".join(kept))
            continue
        revised_lines.append(line)
    return "\n".join(revised_lines).strip()


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _markdown_divider(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def evidence_facts_from_result(result: Mapping[str, Any]) -> tuple[EvidenceFact, ...]:
    markdown_response = result.get("markdown_response")
    if isinstance(markdown_response, Mapping):
        serialized = markdown_response.get("evidence")
        if isinstance(serialized, (list, tuple)):
            facts: list[EvidenceFact] = []
            for item in serialized:
                if not isinstance(item, Mapping):
                    continue
                try:
                    facts.append(EvidenceFact(**dict(item)))
                except (TypeError, ValueError):
                    continue
            if facts:
                return tuple(facts)
    calls = result.get("tool_calls")
    data_md = ""
    if isinstance(markdown_response, Mapping):
        data_md = str(markdown_response.get("data_md") or "")
    return evidence_from_calls(list(calls) if isinstance(calls, list) else [], data_md)


def expected_entities_from_result(question: str, result: Mapping[str, Any]) -> tuple[str, ...]:
    entities: list[str] = []
    explicit = explicit_disease_code(question)
    if explicit:
        entities.append(explicit)
    elif "환자수" in question_metrics(question):
        exact_disease_code = hira_disease_code_for_exact_name(question)
        if exact_disease_code:
            entities.append(exact_disease_code)

    diagnostics = result.get("router_diagnostics")
    routing_v4 = diagnostics.get("routing_v4") if isinstance(diagnostics, Mapping) else None
    proposal = routing_v4.get("proposed_routing_signature") if isinstance(routing_v4, Mapping) else None
    calls = proposal.get("proposed_calls") if isinstance(proposal, Mapping) else None
    if isinstance(calls, (list, tuple)):
        for call in calls:
            args = call.get("normalized_args") if isinstance(call, Mapping) else None
            if not isinstance(args, Mapping):
                continue
            for key in IDENTIFIER_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    entities.append(value.strip())
    return tuple(dict.fromkeys(entities))
