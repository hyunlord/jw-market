from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
import hashlib
import re
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    CODE_RE,
    NUMBER_RE,
    normalize_number,
)
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade
from jw_chat_agent_poc.service.evidence_binding import (
    BindingVerification,
    _matches_display_rounding,
)
from jw_chat_agent_poc.service.evidence_binding_rules import (
    binding_claim_number_tokens,
    claim_metrics_for_token,
    entity_matches,
    expected_entity_set,
    explicit_periods,
    grade,
    has_binding_metadata,
    metric_matches,
    operand_binding_outcome,
    period_matches,
    question_metrics,
    question_view_scopes,
    scope_matches,
    token_unit,
    unit_matches,
    without_bound_identifiers,
)

_MAX_OCCURRENCES: Final = 8
_MAX_FACT_REFS: Final = 8
_MAX_METRIC_SUMMARIES: Final = 8
_MAX_AXIS_SUMMARIES: Final = 4
_MAX_BLOCKED_TOKEN_REFS: Final = 16
_MAX_TEXT_FRAGMENTS: Final = 8
_TEXT_CONTEXT_RADIUS: Final = 96
_MAX_TEXT_FRAGMENT_CHARS: Final = 256
_MAX_TEXT_PROJECTION_CHARS: Final = 2_048


def binding_pipeline_observability(
    *,
    question: str,
    answer: str,
    facts: Sequence[EvidenceFact],
    expected_entities: Sequence[str],
    expected_market_ids: frozenset[str],
    gate: BindingVerification,
    fact_input: Mapping[str, Any],
) -> dict[str, Any]:
    expected = expected_entity_set(question, expected_entities)
    requested_periods = explicit_periods(question)
    expected_scopes = question_view_scopes(question)
    claim_text = without_bound_identifiers(answer, expected)
    occurrences = _claim_occurrences(claim_text)
    if not occurrences:
        return {}

    facts_by_id = {fact.fact_id: fact for fact in facts}
    rejection_reasons = {
        rejection.token: rejection.reason
        for rejection in gate.rejections
    }
    prioritized_occurrences = _prioritize_blocked_occurrences(
        occurrences,
        gate.blocked_numbers,
    )
    projected = tuple(
        _occurrence_trace(
            occurrence,
            answer=claim_text,
            question=question,
            facts=facts,
            facts_by_id=facts_by_id,
            expected=expected,
            requested_periods=requested_periods,
            expected_scopes=expected_scopes,
            expected_market_ids=expected_market_ids,
            rejection_reasons=rejection_reasons,
            blocked_numbers=set(gate.blocked_numbers),
        )
        for occurrence in prioritized_occurrences[:_MAX_OCCURRENCES]
    )
    blocked_refs = tuple(
        _blocked_token_ref(
            token,
            tuple(
                item["occurrence_id"]
                for item in occurrences
                if item["token_ref"] == _hash_ref("token", token)
            ),
        )
        for token in gate.blocked_numbers[:_MAX_BLOCKED_TOKEN_REFS]
    )
    all_occurrence_token_refs = {
        str(item["token_ref"])
        for item in occurrences
    }
    return {
        "pipeline": {
            "fact_input": dict(fact_input),
            "actual_stages": (
                "evidence_deserialization_or_reconstruction",
                "facts_loaded",
                "binding_metadata_eligible",
                "value_candidates",
                "axis_compatible",
                "source_grade_usable",
                "operand_usable",
            ),
            "standalone_stages_not_present": (
                "normalization",
                "deduplication",
                "scope_prefilter",
            ),
            "decision_unit": "unique_token_string",
        },
        "fact_inventory": _stage_inventory(facts),
        "occurrence_count": len(occurrences),
        "occurrences": projected,
        "occurrences_emitted": len(projected),
        "occurrences_truncated": len(occurrences) > _MAX_OCCURRENCES,
        "binder_input": {
            "basis": "claim_text_after_expected_identifier_removal",
            "chars": len(claim_text),
            "utf8_bytes": len(claim_text.encode()),
            "sha256": hashlib.sha256(claim_text.encode()).hexdigest(),
            "truncated": False,
            "source_answer": {
                "chars": len(answer),
                "utf8_bytes": len(answer.encode()),
                "sha256": hashlib.sha256(answer.encode()).hexdigest(),
            },
            "blocked_token_count": len(gate.blocked_numbers),
            "blocked_token_refs": blocked_refs,
            "blocked_token_refs_truncated": (
                len(gate.blocked_numbers) > _MAX_BLOCKED_TOKEN_REFS
            ),
            "blocked_tokens_covered": all(
                _hash_ref("token", token) in all_occurrence_token_refs
                for token in gate.blocked_numbers
            ),
        },
    }


def binding_text_observability(
    *,
    question: str,
    answer: str,
    expected_entities: Sequence[str],
    gate: BindingVerification,
    text_projection_allowed: bool,
) -> dict[str, Any]:
    if not gate.blocked_numbers:
        return {}
    expected = expected_entity_set(question, expected_entities)
    claim_text = without_bound_identifiers(answer, expected)
    return {
        "binder_input_text": _blocked_context_projection(
            claim_text,
            _claim_occurrences(claim_text),
            gate.blocked_numbers,
            allowed=text_projection_allowed,
        ),
        "pre_binding_answer_text": _blocked_context_projection(
            answer,
            _claim_occurrences(answer),
            gate.blocked_numbers,
            allowed=text_projection_allowed,
        ),
    }


def _prioritize_blocked_occurrences(
    occurrences: Sequence[Mapping[str, Any]],
    blocked_numbers: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
    blocked_refs = tuple(_hash_ref("token", token) for token in blocked_numbers)
    selected: list[Mapping[str, Any]] = []
    selected_ids: set[str] = set()
    for token_ref in blocked_refs:
        occurrence = next(
            (
                item
                for item in occurrences
                if item["token_ref"] == token_ref
            ),
            None,
        )
        if occurrence is None:
            continue
        selected.append(occurrence)
        selected_ids.add(str(occurrence["occurrence_id"]))
    selected.extend(
        item
        for item in occurrences
        if str(item["occurrence_id"]) not in selected_ids
    )
    return tuple(selected)


def _blocked_context_projection(
    text: str,
    occurrences: Sequence[Mapping[str, Any]],
    blocked_numbers: Sequence[str],
    *,
    allowed: bool,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scope": "blocked_token_contexts",
        "available": allowed and bool(blocked_numbers),
        "omitted_reason": "",
        "source_text_included_in_full": False,
        "fragments": (),
        "fragment_count": 0,
        "fragments_truncated": False,
        "emitted_chars": 0,
    }
    if not allowed:
        base["available"] = False
        base["omitted_reason"] = "file_grounded_answer"
        return base
    if not blocked_numbers:
        base["available"] = False
        base["omitted_reason"] = "no_blocked_tokens"
        return base

    occurrences_by_ref = {
        str(item["token_ref"]): item
        for item in reversed(occurrences)
    }
    blocked_occurrences = tuple(
        occurrences_by_ref[token_ref]
        for token_ref in dict.fromkeys(
            _hash_ref("token", token) for token in blocked_numbers
        )
        if token_ref in occurrences_by_ref
    )
    if not blocked_occurrences:
        base["available"] = False
        base["omitted_reason"] = "blocked_token_not_found"
        return base

    fragments: list[dict[str, Any]] = []
    emitted_chars = 0
    for occurrence in blocked_occurrences:
        if len(fragments) >= _MAX_TEXT_FRAGMENTS:
            break
        start = max(0, int(occurrence["start"]) - _TEXT_CONTEXT_RADIUS)
        end = min(len(text), int(occurrence["end"]) + _TEXT_CONTEXT_RADIUS)
        fragment = text[start:end]
        if len(fragment) > _MAX_TEXT_FRAGMENT_CHARS:
            fragment = fragment[:_MAX_TEXT_FRAGMENT_CHARS]
            end = start + len(fragment)
        remaining = _MAX_TEXT_PROJECTION_CHARS - emitted_chars
        if remaining <= 0:
            break
        if len(fragment) > remaining:
            fragment = fragment[:remaining]
            end = start + len(fragment)
        if fragment == text and len(fragment) > int(occurrence["end"]) - int(
            occurrence["start"]
        ):
            if int(occurrence["start"]) > 0:
                start += 1
                fragment = text[start:end]
            else:
                end -= 1
                fragment = text[start:end]
        fragments.append(
            {
                "token_ref": occurrence["token_ref"],
                "occurrence_id": occurrence["occurrence_id"],
                "char_range": (occurrence["start"], occurrence["end"]),
                "context_char_range": (start, end),
                "text": fragment,
                "leading_truncated": start > 0,
                "trailing_truncated": end < len(text),
            }
        )
        emitted_chars += len(fragment)

    base["fragments"] = tuple(fragments)
    base["fragment_count"] = len(fragments)
    base["fragments_truncated"] = len(fragments) < len(
        dict.fromkeys(blocked_numbers)
    )
    base["emitted_chars"] = emitted_chars
    return base


def _blocked_token_ref(
    token: str,
    occurrence_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "token_ref": _hash_ref("token", token),
        "occurrence_count": len(occurrence_ids),
        "occurrence_ids": occurrence_ids[:_MAX_OCCURRENCES],
        "occurrence_ids_truncated": len(occurrence_ids) > _MAX_OCCURRENCES,
    }


def evidence_fact_input_inventory(
    result: Mapping[str, Any],
    facts: Sequence[EvidenceFact],
) -> dict[str, Any]:
    markdown_response = result.get("markdown_response")
    if isinstance(markdown_response, Mapping):
        serialized = markdown_response.get("evidence")
        if isinstance(serialized, (list, tuple)) and serialized:
            valid_count = 0
            for item in serialized:
                if not isinstance(item, Mapping):
                    continue
                try:
                    EvidenceFact(**dict(item))
                except (TypeError, ValueError):
                    continue
                valid_count += 1
            if valid_count:
                discarded_count = len(serialized) - valid_count
                return {
                    "source": "serialized_markdown_evidence",
                    "input_item_count": len(serialized),
                    "loaded_fact_count": len(facts),
                    "discarded_count": discarded_count,
                    "discard_reason": (
                        "malformed_serialized_fact" if discarded_count else ""
                    ),
                }
    calls = result.get("tool_calls")
    return {
        "source": "reconstructed_from_tool_calls",
        "input_item_count": len(calls) if isinstance(calls, list) else 0,
        "loaded_fact_count": len(facts),
        "discarded_count": 0,
        "discard_reason": "",
    }


def _claim_occurrences(text: str) -> tuple[dict[str, Any], ...]:
    binding_tokens = set(binding_claim_number_tokens(text))
    if not binding_tokens:
        return ()

    matches: list[tuple[int, int, str]] = []
    for pattern in (NUMBER_RE, CODE_RE):
        for match in pattern.finditer(text):
            raw = match.group(0)
            token = (
                normalize_number(raw)
                if pattern is NUMBER_RE
                else raw.upper()
            )
            if token in binding_tokens:
                matches.append((match.start(), match.end(), token))
    for token in binding_tokens:
        if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2]|Q[1-4])", token, re.IGNORECASE):
            continue
        for match in re.finditer(re.escape(token), text, re.IGNORECASE):
            matches.append((match.start(), match.end(), token))

    unique = sorted(set(matches), key=lambda item: (item[0], item[1], item[2]))
    return tuple(
        {
            "token": token,
            "token_ref": _hash_ref("token", token),
            "occurrence_id": _hash_ref(
                "occurrence",
                hashlib.sha256(text.encode()).hexdigest(),
                str(start),
                str(end),
                token,
            ),
            "start": start,
            "end": end,
            "location": _location(text, start),
        }
        for start, end, token in unique
    )


def _occurrence_trace(
    occurrence: Mapping[str, Any],
    *,
    answer: str,
    question: str,
    facts: Sequence[EvidenceFact],
    facts_by_id: Mapping[str, EvidenceFact],
    expected: set[str],
    requested_periods: tuple[str, ...],
    expected_scopes: frozenset[str],
    expected_market_ids: frozenset[str],
    rejection_reasons: Mapping[str, str],
    blocked_numbers: set[str],
) -> dict[str, Any]:
    token = str(occurrence["token"])
    expected_metrics = claim_metrics_for_token(answer, token) or question_metrics(question)
    metadata_eligible = tuple(fact for fact in facts if has_binding_metadata(fact))
    value_candidates = tuple(
        fact
        for fact in metadata_eligible
        if (
            token in fact.allowed_numbers
            or token.upper() in explicit_periods(fact.period)
            or _matches_display_rounding(token, fact)
        )
    )
    axis_compatible = tuple(
        fact
        for fact in value_candidates
        if entity_matches(fact, expected)
        and metric_matches(fact, expected_metrics)
        and period_matches(fact, requested_periods)
        and unit_matches(fact, token)
        and scope_matches(fact, expected_scopes, expected_market_ids)
    )
    source_grade_usable = tuple(
        fact
        for fact in axis_compatible
        if grade(fact) is not SourceGrade.UNVERIFIED
    )
    operand_usable = tuple(
        fact
        for fact in source_grade_usable
        if operand_binding_outcome(fact, facts_by_id)[0] == "pass"
    )
    stages = (
        _stage(
            "facts_loaded",
            facts,
            previous=(),
            removal_reason=lambda _fact: "",
        ),
        _stage(
            "binding_metadata_eligible",
            metadata_eligible,
            previous=facts,
            removal_reason=lambda _fact: "incomplete_binding_metadata",
        ),
        _stage(
            "value_candidates",
            value_candidates,
            previous=metadata_eligible,
            removal_reason=lambda _fact: "value_not_candidate",
        ),
        _stage(
            "axis_compatible",
            axis_compatible,
            previous=value_candidates,
            removal_reason=lambda fact: _axis_mismatch_reason(
                fact,
                token=token,
                expected=expected,
                expected_metrics=expected_metrics,
                requested_periods=requested_periods,
                expected_scopes=expected_scopes,
                expected_market_ids=expected_market_ids,
            ),
        ),
        _stage(
            "source_grade_usable",
            source_grade_usable,
            previous=axis_compatible,
            removal_reason=lambda _fact: "source_grade",
        ),
        _stage(
            "operand_usable",
            operand_usable,
            previous=source_grade_usable,
            removal_reason=lambda fact: operand_binding_outcome(fact, facts_by_id)[1]
            or "operand_binding",
        ),
    )
    blocked = token in blocked_numbers
    return {
        "occurrence_id": occurrence["occurrence_id"],
        "token_ref": occurrence["token_ref"],
        "token_length": len(token),
        "unit": token_unit(token),
        "char_range": (occurrence["start"], occurrence["end"]),
        "location": occurrence["location"],
        "expected": {
            "basis": "token_global_current_binder",
            "entity": tuple(sorted(expected)),
            "metric": expected_metrics,
            "period": requested_periods,
            "unit": token_unit(token),
            "view": tuple(sorted(expected_scopes)),
            "market_id": tuple(sorted(expected_market_ids)),
        },
        "candidate_fact_refs": tuple(
            _fact_ref(fact)
            for fact in value_candidates[:_MAX_FACT_REFS]
        ),
        "candidate_count": len(value_candidates),
        "candidates_truncated": len(value_candidates) > _MAX_FACT_REFS,
        "stages": stages,
        "decision": "blocked" if blocked else "pass",
        "decision_scope": "unique_token_string",
        "reason": rejection_reasons.get(token, "") if blocked else "",
    }


def _stage(
    name: str,
    facts: Sequence[EvidenceFact],
    *,
    previous: Sequence[EvidenceFact],
    removal_reason: Callable[[EvidenceFact], str],
) -> dict[str, Any]:
    current_refs = {_fact_ref(fact) for fact in facts}
    metric_counts = Counter(fact.metric or "<missing>" for fact in facts)
    axis_counts = _axis_counts(facts)
    removed = tuple(
        {
            "fact_ref": _fact_ref(fact),
            "reason": removal_reason(fact),
        }
        for fact in previous
        if _fact_ref(fact) not in current_refs
    )
    return {
        "stage": name,
        "fact_count": len(facts),
        "metric_counts": tuple(sorted(metric_counts.items()))[:_MAX_METRIC_SUMMARIES],
        "metric_counts_truncated": len(metric_counts) > _MAX_METRIC_SUMMARIES,
        "axis_combinations": _axis_combinations(axis_counts),
        "axis_combinations_truncated": len(axis_counts) > _MAX_AXIS_SUMMARIES,
        "removed_count": len(removed),
        "removed": removed[:_MAX_FACT_REFS],
        "removed_truncated": len(removed) > _MAX_FACT_REFS,
    }


def _stage_inventory(facts: Sequence[EvidenceFact]) -> dict[str, Any]:
    metric_counts = Counter(fact.metric or "<missing>" for fact in facts)
    axis_counts = _axis_counts(facts)
    return {
        "fact_count": len(facts),
        "metric_counts": tuple(sorted(metric_counts.items()))[:_MAX_METRIC_SUMMARIES],
        "metric_counts_truncated": len(metric_counts) > _MAX_METRIC_SUMMARIES,
        "axis_combinations": _axis_combinations(axis_counts),
        "axis_combinations_truncated": len(axis_counts) > _MAX_AXIS_SUMMARIES,
    }


def _axis_counts(
    facts: Sequence[EvidenceFact],
) -> Counter[tuple[str, str, str, str, str, str]]:
    return Counter(
        (
            fact.entity,
            fact.metric,
            fact.period,
            fact.unit,
            fact.view,
            fact.market_id,
        )
        for fact in facts
    )


def _axis_combinations(
    counts: Counter[tuple[str, str, str, str, str, str]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "entity": axes[0],
            "metric": axes[1],
            "period": axes[2],
            "unit": axes[3],
            "view": axes[4],
            "market_id": axes[5],
            "count": count,
        }
        for axes, count in sorted(counts.items())[:_MAX_AXIS_SUMMARIES]
    )


def _axis_mismatch_reason(
    fact: EvidenceFact,
    *,
    token: str,
    expected: set[str],
    expected_metrics: tuple[str, ...],
    requested_periods: tuple[str, ...],
    expected_scopes: frozenset[str],
    expected_market_ids: frozenset[str],
) -> str:
    mismatches = tuple(
        axis
        for axis, matches in (
            ("entity", entity_matches(fact, expected)),
            ("metric", metric_matches(fact, expected_metrics)),
            ("period", period_matches(fact, requested_periods)),
            ("unit", unit_matches(fact, token)),
            ("view", scope_matches(fact, expected_scopes)),
            ("market_id", scope_matches(fact, frozenset(), expected_market_ids)),
        )
        if not matches
    )
    return "+".join(mismatches) or "axis_mismatch"


def _fact_ref(fact: EvidenceFact) -> str:
    return _hash_ref(
        "fact",
        fact.fact_id,
        fact.entity,
        fact.metric,
        fact.period,
        fact.unit,
        fact.view,
        fact.market_id,
        fact.tool,
        fact.path,
    )


def _location(text: str, start: int) -> dict[str, Any]:
    line_index = text.count("\n", 0, start)
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    heading = ""
    heading_level = 0
    for previous in reversed(text[:line_start].splitlines()):
        match = re.match(r"^(#{1,6})\s+(.+)$", previous.strip())
        if match:
            heading_level = len(match.group(1))
            heading = match.group(2)
            break
    base = {
        "line": line_index,
        "section_ref": _hash_ref("section", heading) if heading else "",
        "section_level": heading_level,
    }
    if line.strip().startswith("|") and line.strip().endswith("|"):
        column = line[: start - line_start].count("|")
        table_start = line_index
        lines = text.splitlines()
        while table_start > 0 and lines[table_start - 1].strip().startswith("|"):
            table_start -= 1
        return {
            **base,
            "kind": "table",
            "table_row": line_index - table_start,
            "table_column": max(column - 1, 0),
        }
    sentence = len(re.findall(r"[.!?。]\s+", line[: start - line_start]))
    return {**base, "kind": "prose", "sentence": sentence}


def _hash_ref(namespace: str, *parts: str) -> str:
    payload = "\x1f".join((namespace, *parts)).encode()
    return hashlib.sha256(payload).hexdigest()[:16]
