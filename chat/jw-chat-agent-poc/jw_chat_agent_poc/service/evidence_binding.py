from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Final

from jw_chat_agent_poc.orchestrator.hira_disease import hira_disease_code_for_exact_name
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact, evidence_from_calls, number_tokens
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.service.evidence_binding_diagnostics import (
    ClaimRejectionDiagnostic,
    rejection_diagnostic,
)
from jw_chat_agent_poc.service.failure_disposition import failure_kind as detect_failure_kind
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
    question_view_scopes,
    requested_period_unavailable,
    scope_matches,
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
    failure_kind: str | None = None
    rejections: tuple[ClaimRejectionDiagnostic, ...] = ()
    # --- observation only -------------------------------------------------
    # Which return site produced this verdict, and how the token loop scored.
    # Nothing in this module reads these back: they never reach a branch, a
    # comparison, or a returned answer. Measurement does not vote.
    #
    # Counts are None -- not 0 -- when the return happened before the token
    # loop ran. "not observed" and "observed, and it was zero" are different
    # facts and a reader must be able to tell them apart.
    decision_site: str | None = None
    substitution_triggered: bool = False
    bind_attempted_count: int | None = None
    bind_succeeded_count: int | None = None
    blocked_reason_histogram: tuple[tuple[str, int], ...] | None = None


def _reason_histogram(reasons: Sequence[str]) -> tuple[tuple[str, int], ...] | None:
    """Count blocked reasons, preserving first-seen order. None when empty."""

    if not reasons:
        return None
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return tuple(counts.items())


def verify_claim_bindings(
    *,
    question: str,
    answer: str,
    facts: Sequence[EvidenceFact],
    expected_entities: Sequence[str] = (),
    expected_market_ids: frozenset[str] = frozenset(),
    _allow_partial_exclusion: bool = True,
) -> BindingVerification:
    expected = expected_entity_set(question, expected_entities)
    metrics = question_metrics(question)
    requested_periods = explicit_periods(question)
    expected_scopes = question_view_scopes(question)
    detected_failure_kind = detect_failure_kind(answer)
    if detected_failure_kind:
        return BindingVerification(
            answer=answer,
            status="fail",
            disposition="unavailable",
            blocked_claim_count=0,
            blocked_reasons=(f"FAILURE_KIND_{detected_failure_kind.upper()}",),
            blocked_numbers=(),
            failure_kind=detected_failure_kind,
            decision_site="failure_kind_passthrough",
        )
    if "환자수" in metrics and not expected:
        blocked_numbers = claim_number_tokens(answer)
        rejections = tuple(
            rejection_diagnostic(
                token=token,
                reason="MISSING_EXPECTED_ENTITY_BINDING",
                candidates=tuple(
                    fact
                    for fact in facts
                    if has_binding_metadata(fact)
                    and (
                        token in fact.allowed_numbers
                        or token.upper() in explicit_periods(fact.period)
                        or _matches_display_rounding(token, fact)
                    )
                ),
                expected_entities=expected,
                expected_metrics=metrics,
                requested_periods=requested_periods,
                expected_scopes=expected_scopes,
                expected_market_ids=expected_market_ids,
                forced_mismatch_axes=("entity",),
            )
            for token in blocked_numbers
        )
        return BindingVerification(
            answer=_BINDING_FAILURE_ANSWER,
            status="fail",
            disposition="unavailable",
            blocked_claim_count=len(blocked_numbers),
            blocked_reasons=("MISSING_EXPECTED_ENTITY_BINDING",),
            blocked_numbers=blocked_numbers,
            rejections=rejections,
            decision_site="missing_expected_entity_binding",
            substitution_triggered=True,
            blocked_reason_histogram=_reason_histogram(
                ("MISSING_EXPECTED_ENTITY_BINDING",) * len(blocked_numbers)
            ),
        )
    if not expected and not metrics:
        return BindingVerification(
            answer=answer,
            status="pass",
            disposition="answered",
            blocked_claim_count=0,
            blocked_reasons=(),
            blocked_numbers=(),
            decision_site="no_expected_no_metrics_pass",
        )
    facts_by_id = {fact.fact_id: fact for fact in facts}
    claim_text = without_bound_identifiers(
        answer,
        expected.union(expected_market_ids),
    )

    blocked: list[str] = []
    blocked_numbers: list[str] = []
    partial_reasons: list[str] = []
    rejections: list[ClaimRejectionDiagnostic] = []
    attempted_count = 0
    succeeded_count = 0
    for token in binding_claim_number_tokens(claim_text):
        attempted_count += 1
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
                rejections.append(
                    rejection_diagnostic(
                        token=token,
                        reason="MISSING_EVIDENCE_BINDING",
                        candidates=(),
                        expected_entities=expected,
                        expected_metrics=metrics,
                        requested_periods=requested_periods,
                        expected_scopes=expected_scopes,
                        expected_market_ids=expected_market_ids,
                        forced_mismatch_axes=("evidence",),
                    )
                )
            continue

        claim_metrics = claim_metrics_for_token(claim_text, token) or metrics

        matching = tuple(
            fact
            for fact in candidates
            if entity_matches(fact, expected)
            and metric_matches(fact, claim_metrics)
            and period_matches(fact, requested_periods)
            and unit_matches(fact, token)
            and scope_matches(fact, expected_scopes, expected_market_ids)
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
                and scope_matches(fact, expected_scopes, expected_market_ids)
            )
            if (
                requested_periods
                and period_compatible_except_period
                and requested_period_unavailable(answer, requested_periods)
            ):
                partial_reasons.append("REQUESTED_PERIOD_UNAVAILABLE")
                continue
            reason = mismatch_reason(
                candidates,
                expected,
                claim_metrics,
                requested_periods=requested_periods,
                token=token,
                expected_scopes=expected_scopes,
                expected_market_ids=expected_market_ids,
            )
            blocked.append(reason)
            blocked_numbers.append(token)
            rejections.append(
                rejection_diagnostic(
                    token=token,
                    reason=reason,
                    candidates=candidates,
                    expected_entities=expected,
                    expected_metrics=claim_metrics,
                    requested_periods=requested_periods,
                    expected_scopes=expected_scopes,
                    expected_market_ids=expected_market_ids,
                )
            )
            continue

        grade_usable = tuple(fact for fact in matching if grade(fact) is not SourceGrade.UNVERIFIED)
        if not grade_usable:
            blocked.append("SOURCE_GRADE_MISMATCH")
            blocked_numbers.append(token)
            rejections.append(
                rejection_diagnostic(
                    token=token,
                    reason="SOURCE_GRADE_MISMATCH",
                    candidates=matching,
                    expected_entities=expected,
                    expected_metrics=claim_metrics,
                    requested_periods=requested_periods,
                    expected_scopes=expected_scopes,
                    expected_market_ids=expected_market_ids,
                    forced_mismatch_axes=("source_grade",),
                )
            )
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
            reason = (
                operand_failure_reasons[0]
                if operand_failure_reasons
                else "OPERAND_BINDING_MISMATCH"
            )
            blocked.append(reason)
            blocked_numbers.append(token)
            rejections.append(
                rejection_diagnostic(
                    token=token,
                    reason=reason,
                    candidates=grade_usable,
                    expected_entities=expected,
                    expected_metrics=claim_metrics,
                    requested_periods=requested_periods,
                    expected_scopes=expected_scopes,
                    expected_market_ids=expected_market_ids,
                    forced_mismatch_axes=("operands",),
                )
            )
            continue
        succeeded_count += 1
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
                    expected_market_ids=expected_market_ids,
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
                        rejections=tuple((*rejections, *remainder.rejections)),
                        decision_site="partial_exclusion_rescue",
                        bind_attempted_count=attempted_count,
                        bind_succeeded_count=succeeded_count,
                        blocked_reason_histogram=_reason_histogram(blocked),
                    )
        return BindingVerification(
            answer=_BINDING_FAILURE_ANSWER,
            status="fail",
            disposition="unavailable",
            blocked_claim_count=len(unique_blocked_numbers),
            blocked_reasons=blocked_reasons,
            blocked_numbers=unique_blocked_numbers,
            rejections=tuple(rejections),
            decision_site="blocked_substitution",
            substitution_triggered=True,
            bind_attempted_count=attempted_count,
            bind_succeeded_count=succeeded_count,
            blocked_reason_histogram=_reason_histogram(blocked),
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
            decision_site="partial_metadata_notice",
            bind_attempted_count=attempted_count,
            bind_succeeded_count=succeeded_count,
            blocked_reason_histogram=_reason_histogram(blocked),
        )

    return BindingVerification(
        answer=answer,
        status="pass",
        disposition="answered",
        blocked_claim_count=0,
        blocked_reasons=(),
        blocked_numbers=(),
        decision_site="clean_pass",
        bind_attempted_count=attempted_count,
        bind_succeeded_count=succeeded_count,
        blocked_reason_histogram=_reason_histogram(blocked),
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


def expected_market_ids_from_result(result: Mapping[str, Any]) -> frozenset[str]:
    """Return internal market identifiers pinned by resolution or routing."""
    resolution = result.get("resolution")
    if isinstance(resolution, Mapping):
        resolved_ids = {
            normalized
            for key in ("market_id", "atc4_code")
            if (normalized := _normalized_market_id(resolution.get(key)))
        }
        atc4_codes = resolution.get("atc4_codes")
        if isinstance(atc4_codes, (list, tuple)):
            resolved_ids.update(
                normalized
                for value in atc4_codes
                if (normalized := _normalized_market_id(value))
            )
        if resolved_ids:
            return frozenset(resolved_ids)

    market_ids: set[str] = set()
    diagnostics = result.get("router_diagnostics")
    routing_v4 = diagnostics.get("routing_v4") if isinstance(diagnostics, Mapping) else None
    proposal = routing_v4.get("proposed_routing_signature") if isinstance(routing_v4, Mapping) else None
    calls = proposal.get("proposed_calls") if isinstance(proposal, Mapping) else None
    if not isinstance(calls, (list, tuple)):
        return frozenset()
    for call in calls:
        args = call.get("normalized_args") if isinstance(call, Mapping) else None
        if not isinstance(args, Mapping):
            continue
        for key in ("market_id", "ml_id", "cd_id"):
            normalized = _normalized_market_id(args.get(key))
            if normalized:
                market_ids.add(normalized)
    return frozenset(market_ids)


def _normalized_market_id(value: object) -> str:
    if isinstance(value, str | int) and not isinstance(value, bool):
        return str(value).strip().casefold()
    return ""
