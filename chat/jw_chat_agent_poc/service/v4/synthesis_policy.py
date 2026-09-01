from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, QueryScope, SourceName
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.temporal_analysis import (
    clinical_time_axis,
    nedrug_time_axis,
    patent_time_axis,
)

# The live LB hard limit is 600s. Keep a 15% upstream safety margin and align
# browser, BFF, route, and request handling on a 510s shared budget.
_DEFAULT_TOTAL_REQUEST_BUDGET_S = 510.0
_DEFAULT_MAX_SYNTHESIS_BUDGET_S = 75.0
_DEFAULT_MIN_SYNTHESIS_BUDGET_S = 15.0
# Reserve 30s for assembly and finalization while giving the parallel section
# ladder enough room for observed 106-220s generation attempts.
_DEFAULT_INSIGHT_LANE_TIMEOUT_S = 240.0
_DEFAULT_PROMPT_CHAR_LIMIT = 120_000
_DEFAULT_SOURCE_RENDER_LIMIT = 40
_HIRA_SOURCE_RENDER_LIMIT = 160


@dataclass(frozen=True)
class SynthesisPolicy:
    total_request_budget_s: float
    max_synthesis_budget_s: float
    min_synthesis_budget_s: float
    prompt_char_limit: int
    source_render_limit: int

    @classmethod
    def from_env(cls) -> SynthesisPolicy:
        policy = cls(
            total_request_budget_s=_float_env(
                "CHAT_V4_TOTAL_REQUEST_BUDGET_S", _DEFAULT_TOTAL_REQUEST_BUDGET_S
            ),
            max_synthesis_budget_s=_float_env(
                "CHAT_V4_MAX_SYNTHESIS_BUDGET_S", _DEFAULT_MAX_SYNTHESIS_BUDGET_S
            ),
            min_synthesis_budget_s=_float_env(
                "CHAT_V4_MIN_SYNTHESIS_BUDGET_S", _DEFAULT_MIN_SYNTHESIS_BUDGET_S
            ),
            prompt_char_limit=_int_env(
                "CHAT_V4_SYNTHESIS_PROMPT_CHAR_LIMIT", _DEFAULT_PROMPT_CHAR_LIMIT
            ),
            source_render_limit=_int_env(
                "CHAT_V4_SOURCE_RENDER_LIMIT", _DEFAULT_SOURCE_RENDER_LIMIT
            ),
        )
        if policy.min_synthesis_budget_s > policy.max_synthesis_budget_s:
            raise ValueError("minimum synthesis budget exceeds maximum")
        return policy

    def allocate_synthesis_budget(self, *, remaining_s: float) -> float | None:
        if remaining_s < self.min_synthesis_budget_s:
            return None
        return min(self.max_synthesis_budget_s, remaining_s)

    @property
    def insight_lane_timeout_s(self) -> float:
        return _float_env(
            "CHAT_V4_INSIGHT_LANE_TIMEOUT_S", _DEFAULT_INSIGHT_LANE_TIMEOUT_S
        )


# Keys whose values are the per-source packets the prompt is built from. A budget
# has to be shared between these, because they are what one oversized lane crowds out.
_SOURCE_PACKET_KEYS = ("external_evidence", "internal_datamart", "internal_deep_analysis")


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _packet_label(packet: Any, index: int) -> str:
    if isinstance(packet, Mapping):
        for key in ("source", "name"):
            label = packet.get(key)
            if isinstance(label, str) and label.strip():
                return label.strip()
    return f"packet_{index}"


def _largest_record_list(value: Any) -> tuple[list[Any] | None, Any, Any]:
    """Find the bulkiest list inside a packet, with its parent and key.

    Shape-agnostic on purpose: a packet carries an upstream payload under
    ``detail`` and an evidence envelope whose record list has moved between
    schema versions. Searching by size rather than by a hard-coded path means a
    future envelope change degrades the trim, not the answer.
    """
    best: tuple[int, list[Any] | None, Any, Any] = (0, None, None, None)

    def walk(node: Any, parent: Any, key: Any) -> None:
        nonlocal best
        if isinstance(node, list):
            size = _size(node)
            if size > best[0] and len(node) > 1:
                best = (size, node, parent, key)
            for item in node:
                walk(item, node, None)
        elif isinstance(node, Mapping):
            for child_key, child in node.items():
                walk(child, node, child_key)

    walk(value, None, None)
    return best[1], best[2], best[3]


def _shrink_packet(packet: Any, budget: int) -> tuple[Any, dict[str, Any]]:
    """Bring one source's packet under its share, reporting exactly what left.

    Nothing is discarded from the turn: the full payload stays in the evidence
    sets and the inspection detail. Only this lane's copy inside the prompt is
    reduced, and only as far as its own share requires.
    """
    if not isinstance(packet, Mapping) or _size(packet) <= budget:
        return packet, {}
    current: dict[str, Any] = dict(packet)
    trimmed: dict[str, Any] = {"budget_chars": budget, "before_chars": _size(packet)}

    detail = current.get("detail")
    if detail is not None and not (
        isinstance(detail, Mapping) and "omitted" in detail
    ):
        detail_chars = _size(detail)
        current["detail"] = {
            "omitted": "per-source prompt budget",
            "omitted_chars": detail_chars,
            "retained_in": "inspection_detail",
        }
        trimmed["detail_chars_omitted"] = detail_chars
        if _size(current) <= budget:
            trimmed["after_chars"] = _size(current)
            return current, trimmed

    # Still oversized: thin the bulkiest record list, halving until it fits so a
    # lane keeps a representative sample rather than losing its content entirely.
    for _ in range(24):
        if _size(current) <= budget:
            break
        records, parent, key = _largest_record_list(current)
        if records is None or parent is None or key is None or len(records) <= 1:
            break
        kept = max(1, len(records) // 2)
        trimmed["records_withheld_from_prompt"] = (
            trimmed.get("records_withheld_from_prompt", 0) + (len(records) - kept)
        )
        parent[key] = records[:kept]
        trimmed["records_in_prompt"] = kept
    trimmed["after_chars"] = _size(current)
    return current, trimmed


def bound_sources_fairly(value: Any, *, char_limit: int) -> tuple[Any, dict[str, Any]]:
    """Give every source a share of the prompt before anyone takes all of it.

    The global bound this runs ahead of is all-or-nothing: once the prompt is over
    the limit it replaces *every* record with a bare identifier list, so a single
    8.4MB lane costs the other six their content. One live turn spent 8,569,677
    chars on 1,004 clinical trials and the synthesis, left with identifiers only,
    wrote that the trials "were not confirmed in the provided evidence".

    Sharing is proportional-with-redistribution: packets already under an equal
    share keep everything and hand their slack to the ones over it, so trimming
    reaches only the lanes that are actually crowding the prompt.
    """
    if char_limit <= 0:
        raise ValueError("char_limit must be positive")
    if not isinstance(value, Mapping):
        return value, {"applied": False, "reason": "prompt_is_not_a_mapping"}

    packets: list[tuple[str, int, str, Any]] = []  # key, index, label, packet
    for key in _SOURCE_PACKET_KEYS:
        entries = value.get(key)
        if isinstance(entries, list):
            for index, packet in enumerate(entries):
                packets.append((key, index, _packet_label(packet, index), packet))
    if not packets:
        return value, {"applied": False, "reason": "no_source_packets"}

    overhead = _size(value) - sum(_size(packet) for _k, _i, _l, packet in packets)
    available = max(char_limit - max(overhead, 0), char_limit // 2)

    sizes = {(k, i): _size(p) for k, i, _l, p in packets}
    if sum(sizes.values()) <= available:
        return value, {"applied": False, "reason": "already_within_budget"}

    # Redistribute: repeatedly give every not-yet-capped packet an equal share and
    # let the ones that do not need it release the remainder.
    remaining = available
    uncapped = {(k, i) for k, i, _l, _p in packets}
    budgets: dict[tuple[str, int], int] = {}
    while uncapped:
        share = remaining // len(uncapped)
        fitting = {ident for ident in uncapped if sizes[ident] <= share}
        if not fitting:
            for ident in uncapped:
                budgets[ident] = share
            break
        for ident in fitting:
            budgets[ident] = sizes[ident]
            remaining -= sizes[ident]
        uncapped -= fitting

    updated = dict(value)
    per_source: dict[str, Any] = {}
    for key in _SOURCE_PACKET_KEYS:
        entries = value.get(key)
        if not isinstance(entries, list):
            continue
        rebuilt: list[Any] = []
        for index, packet in enumerate(entries):
            ident = (key, index)
            budget = budgets.get(ident, sizes.get(ident, 0))
            shrunk, trimmed = _shrink_packet(packet, budget)
            rebuilt.append(shrunk)
            if trimmed:
                label = _packet_label(packet, index)
                per_source.setdefault(label, []).append(trimmed)
        updated[key] = rebuilt

    return updated, {
        "applied": bool(per_source),
        "char_limit": char_limit,
        "available_for_packets": available,
        "packet_count": len(packets),
        "sources_trimmed": sorted(per_source),
        "detail": per_source,
    }


def bound_synthesis_messages(
    messages: Sequence[dict[str, str]],
    *,
    char_limit: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if char_limit <= 0:
        raise ValueError("char_limit must be positive")
    copied = [dict(message) for message in messages]
    before = sum(len(message.get("content", "")) for message in copied)
    if before <= char_limit:
        return copied, _prompt_trace(before, before, False, "none")

    # Share the prompt between sources before the all-or-nothing strategies below
    # get a chance to strip every record to an identifier.
    fair_trace: dict[str, Any] = {"applied": False, "reason": "not_attempted"}
    shared: list[dict[str, str]] = [dict(copied[0])]
    for message in copied[1:]:
        content = message.get("content", "")
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            shared.append(dict(message))
            continue
        bounded_value, trace = bound_sources_fairly(parsed, char_limit=char_limit)
        if trace.get("applied"):
            fair_trace = trace
            shared.append(
                {**message, "content": json.dumps(bounded_value, ensure_ascii=False, default=str)}
            )
        else:
            fair_trace = trace
            shared.append(dict(message))
    shared_chars = sum(len(message.get("content", "")) for message in shared)
    if shared_chars <= char_limit:
        trace = _prompt_trace(before, shared_chars, True, "per_source_fair_share")
        trace["fair_share"] = fair_trace
        return shared, trace
    copied = shared

    bounded = [dict(copied[0])]
    for message in copied[1:]:
        content = message.get("content", "")
        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            bounded.append({**message, "content": content[: max(0, char_limit // 2)]})
            continue
        compacted = _compact_value(value, aggressive=False)
        bounded.append(
            {**message, "content": json.dumps(compacted, ensure_ascii=False, default=str)}
        )

    after = sum(len(message.get("content", "")) for message in bounded)
    strategy = "structured_summary"
    if after > char_limit:
        strategy = "identifier_manifest"
        bounded = [dict(copied[0])]
        for message in copied[1:]:
            try:
                value = json.loads(message.get("content", ""))
            except (TypeError, json.JSONDecodeError):
                value = message.get("content", "")
            compacted = _compact_value(value, aggressive=True)
            bounded.append(
                {**message, "content": json.dumps(compacted, ensure_ascii=False, default=str)}
            )
        after = sum(len(message.get("content", "")) for message in bounded)

    if after > char_limit:
        # The system contract remains intact; the dynamic packet becomes a manifest.
        available = max(0, char_limit - len(bounded[0].get("content", "")))
        manifest = _identifier_manifest(messages[1:])
        encoded = json.dumps(manifest, ensure_ascii=False, default=str)
        identifiers = manifest["record_identifiers"]
        while len(encoded) > available and identifiers:
            identifiers = identifiers[: len(identifiers) // 2]
            manifest["record_identifiers"] = identifiers
            manifest["identifiers_omitted_from_prompt"] = (
                manifest["record_count"] - len(identifiers)
            )
            encoded = json.dumps(manifest, ensure_ascii=False, default=str)
        if len(encoded) > available:
            manifest = {
                "prompt_compacted": True,
                "record_count": manifest["record_count"],
                "record_identifiers": [],
                "identifiers_omitted_from_prompt": manifest["record_count"],
            }
            encoded = json.dumps(manifest, ensure_ascii=False, default=str)
        bounded = [bounded[0], {"role": "user", "content": encoded}]
        after = sum(len(message.get("content", "")) for message in bounded)
        strategy = "bounded_identifier_manifest"

    trace = _prompt_trace(before, after, True, strategy)
    # Report the fair-share attempt even when it was not enough on its own, so a
    # turn that still fell through to a manifest says why.
    trace["fair_share"] = fair_trace
    return bounded, trace


def prune_unsupported_source_queries(
    plan: PlannerOutput,
) -> tuple[PlannerOutput, dict[str, Any]]:
    # Off by default. Pruning decides, before any call is made, that a source cannot
    # answer this question — and a wrong guess costs the answer a whole lane with no
    # way for the user to know what was never asked. "리바로젯 제네릭 임상현황" lost mart,
    # nedrug and hira that way, even though mart holds that brand's sales and a generic
    # question is exactly when patent and approval evidence matters. An empty lane is
    # cheap and the shortfall notice already reports it; a lane never attempted is not
    # recoverable. Set CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES=1 to restore the old
    # behaviour.
    if os.environ.get("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", "0").strip().casefold() not in {
        "1",
        "true",
        "on",
        "yes",
    }:
        return plan, {"applied": False, "disabled": True, "omitted": {}}
    requested = frozenset(plan.requested_answer_shape.measure_or_attribute)
    if not requested:
        return plan, {"applied": False, "omitted": {}}

    supported: dict[SourceName, frozenset[str]] = {
        "mart": frozenset({"sales", "market_share", "prescription_volume", "market_metric"}),
        "hira": frozenset({"patient_count", "reimbursement", "visit_days", "medical_cost"}),
        "clinicaltrials": frozenset({"clinical_trials", "active_clinical_trials"}),
        "patent": frozenset({"patent"}),
        "nedrug": frozenset({"approval", "reexamination"}),
        "openfda": frozenset({"safety", "label"}),
        "web": frozenset(requested),
        "document": frozenset(requested),
    }
    updates: dict[str, tuple[str, ...]] = {}
    omitted_trace: dict[str, list[dict[str, str]]] = {}
    previous = plan.query_scope
    requested_calls = dict(previous.requested_calls) if previous else {}
    executed_calls = dict(previous.executed_calls) if previous else {}
    omitted_queries = dict(previous.omitted_queries) if previous else {}
    unexecuted_reasons = dict(previous.unexecuted_reasons) if previous else {}
    for source, queries in plan.tool_queries.items():
        if source in plan.answer_sources or requested & supported[source]:
            continue
        updates[source] = ()
        requested_calls[source] = max(requested_calls.get(source, 0), len(queries))
        executed_calls[source] = 0
        omitted_queries[source] = tuple(
            dict.fromkeys((*omitted_queries.get(source, ()), *queries))
        )
        omitted_trace[source] = [
            {"query": query, "reason": "unsupported_measure"} for query in queries
        ]
    if not updates:
        return plan, {"applied": False, "omitted": {}}
    return (
        plan.model_copy(
            update={
                "tool_queries": plan.tool_queries.model_copy(update=updates),
                "query_scope": QueryScope(
                    requested_calls=requested_calls,
                    executed_calls=executed_calls,
                    omitted_queries=omitted_queries,
                    unexecuted_reasons=unexecuted_reasons,
                ),
            }
        ),
        {"applied": True, "omitted": omitted_trace},
    )


def limit_evidence_sets_for_render(
    evidence_sets: Sequence[EvidenceSet],
    *,
    per_source_limit: int,
    question: str = "",
    observed_on: date | None = None,
) -> tuple[tuple[EvidenceSet, ...], dict[str, Any]]:
    if per_source_limit <= 0:
        raise ValueError("per_source_limit must be positive")
    limited: list[EvidenceSet] = []
    sources: dict[str, dict[str, Any]] = {}
    for evidence in evidence_sets:
        total = len(evidence.records)
        candidates = (
            _dedupe_mart_render_records(evidence.records)
            if evidence.source == "mart"
            else tuple(evidence.records)
        )
        unique_candidates = len(candidates)
        source_limit = (
            max(per_source_limit, _HIRA_SOURCE_RENDER_LIMIT)
            if evidence.source == "hira"
            else per_source_limit
        )
        shown = min(unique_candidates, source_limit)
        duplicates_collapsed = total - unique_candidates
        render_limited = shown < unique_candidates
        if render_limited or duplicates_collapsed:
            if render_limited:
                if evidence.source == "patent":
                    selected = _select_patent_records_for_render(
                        candidates,
                        limit=shown,
                    )
                    selection_rule = (
                        "patent_numbers_then_product_rows_then_evidence_id"
                    )
                    selection_is_ranked = True
                elif evidence.source == "clinicaltrials":
                    selected = _select_clinical_records_for_render(
                        candidates,
                        queries=evidence.query_spec,
                        limit=shown,
                    )
                    selection_rule = "clinical_query_fair_share_then_evidence_id"
                    selection_is_ranked = False
                elif evidence.source == "hira":
                    selected = _select_hira_records_for_render(
                        candidates,
                        queries=evidence.query_spec,
                        question=question,
                        limit=shown,
                    )
                    selection_rule = "hira_code_fair_share_then_query_axis"
                    selection_is_ranked = True
                else:
                    selected, selection_rule, selection_is_ranked = (
                        _select_records_for_question(
                            candidates,
                            question=question,
                            limit=shown,
                        )
                    )
            else:
                selected = candidates
                selection_rule = "all_unique_records_after_duplicate_collapse"
                selection_is_ranked = False
            source_trace = {
                "shown": shown,
                "total": total,
                "selection_rule": selection_rule,
                "selection_is_ranked": selection_is_ranked,
            }
            if evidence.source == "mart":
                source_trace.update(
                    {
                        "unique_candidates": unique_candidates,
                        "duplicates_collapsed": duplicates_collapsed,
                    }
                )
            sources[evidence.source] = source_trace
            # source_refs has to follow records. It did not, and the two surfaces
            # disagreed in front of the user: the notice said "clinicaltrials:
            # 40/1004 표시" while the source block below it listed all 1,004 links,
            # because the block renders from source_refs and only records were cut.
            # Refs that belong to a withheld record go with it; refs that belong to
            # the call rather than to any one record stay, so a lane never loses its
            # own attribution.
            # A ref is attributable when some record claims that url. Those follow
            # their record. Anything no record claims is call-level attribution -
            # the search itself, a landing page - and stays, so the lane never
            # loses the right to say where it looked.
            # Note result_refs() emits one ref per citation, so a lane's per-study
            # urls arrive here twice: once from the record and once from the call.
            # Keying on "does any record claim this url" is what makes the two
            # agree; treating everything outside the withheld records as
            # call-level left all 1,004 links in place.
            kept_refs = {
                ref.url
                for record in selected
                for ref in record.source_refs
            }
            attributable = {
                ref.url for record in evidence.records for ref in record.source_refs
            }
            added_reasons: list[str] = []
            if duplicates_collapsed:
                added_reasons.append("duplicate_projection_collapsed")
            if render_limited:
                added_reasons.append("surface_render_limit")
            coverage = evidence.coverage.model_copy(
                update={
                    "records_unique": unique_candidates,
                    "partial_reasons": tuple(
                        dict.fromkeys(
                            (*evidence.coverage.partial_reasons, *added_reasons)
                        )
                    ),
                }
            )
            retained_refs = tuple(
                ref
                for ref in evidence.source_refs
                if ref.url not in attributable or ref.url in kept_refs
            )
            sources[evidence.source]["refs_shown"] = len(retained_refs)
            sources[evidence.source]["refs_total"] = len(evidence.source_refs)
            full_aggregate = _surface_full_aggregate(
                evidence.source,
                candidates,
                observed_on=observed_on,
            )
            evidence = evidence.model_copy(
                update={
                    "records": selected,
                    "coverage": coverage,
                    "source_refs": retained_refs,
                    **(
                        {
                            "query_manifest": (
                                *evidence.query_manifest,
                                full_aggregate,
                            )
                        }
                        if full_aggregate is not None
                        else {}
                    ),
                }
            )
        limited.append(evidence)
    rules = tuple(
        dict.fromkeys(str(detail["selection_rule"]) for detail in sources.values())
    )
    return tuple(limited), {
        "applied": bool(sources),
        "sources": sources,
        "selection_rule": (
            rules[0] if len(rules) == 1 else "per_source_question_aware_selection"
        )
        if rules
        else "all_records_within_limit",
        "selection_is_ranked": any(
            bool(detail["selection_is_ranked"]) for detail in sources.values()
        ),
    }


def _select_patent_records_for_render(
    records: Sequence[EvidenceRecord],
    *,
    limit: int,
) -> tuple[EvidenceRecord, ...]:
    product_groups: dict[str, list[EvidenceRecord]] = {}
    other_records: list[EvidenceRecord] = []
    brand_scope_applied = any(
        record.payload.get("brand_scope_match") in {True, False}
        for record in records
    )

    def record_key(record: EvidenceRecord) -> tuple[int, str]:
        scope_match = record.payload.get("brand_scope_match")
        scope_rank = (
            0
            if brand_scope_applied and scope_match is True
            else 2
            if brand_scope_applied and scope_match is False
            else 1
        )
        return scope_rank, record.evidence_id

    for record in sorted(records, key=record_key):
        if str(record.payload.get("page_group") or "").strip() != "제품특허":
            other_records.append(record)
            continue
        patent_no = str(record.payload.get("patent_no") or "").strip().casefold()
        group_key = patent_no or f"record:{record.evidence_id}"
        product_groups.setdefault(group_key, []).append(record)

    representatives = sorted(
        (group[0] for group in product_groups.values()),
        key=record_key,
    )
    remaining_products = sorted(
        (record for group in product_groups.values() for record in group[1:]),
        key=record_key,
    )
    other_records.sort(key=record_key)
    return tuple((*representatives, *remaining_products, *other_records)[:limit])


def _select_clinical_records_for_render(
    records: Sequence[EvidenceRecord],
    *,
    queries: Sequence[str],
    limit: int,
) -> tuple[EvidenceRecord, ...]:
    ordered = tuple(sorted(records, key=lambda record: record.evidence_id))
    normalized_queries = tuple(
        dict.fromkeys(" ".join(query.split()).casefold() for query in queries if query.strip())
    )
    if len(normalized_queries) < 2:
        return ordered[:limit]
    queues = {
        query: [
            record
            for record in ordered
            if query in _clinical_record_queries(record)
        ]
        for query in normalized_queries
    }
    positions = {query: 0 for query in normalized_queries}
    selected: list[EvidenceRecord] = []
    seen: set[str] = set()
    while len(selected) < limit:
        progressed = False
        for query in normalized_queries:
            queue = queues[query]
            while positions[query] < len(queue):
                record = queue[positions[query]]
                positions[query] += 1
                if record.evidence_id in seen:
                    continue
                selected.append(record)
                seen.add(record.evidence_id)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    selected.extend(
        record for record in ordered if record.evidence_id not in seen
    )
    return tuple(selected[:limit])


def _clinical_record_queries(record: EvidenceRecord) -> tuple[str, ...]:
    raw = record.payload.get("matched_query")
    values = (raw,) if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else ()
    return tuple(
        dict.fromkeys(" ".join(str(value).split()).casefold() for value in values if str(value).strip())
    )


def _clinical_full_aggregate(
    records: Sequence[EvidenceRecord],
    *,
    observed_on: date | None,
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    sponsor_counts: dict[str, int] = {}
    query_counts: dict[str, int] = {}
    for record in records:
        status = str(record.payload.get("overall_status") or "").strip().upper()
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        raw_phases = record.payload.get("phases")
        phases = (
            (raw_phases,)
            if isinstance(raw_phases, str)
            else raw_phases
            if isinstance(raw_phases, (list, tuple))
            else ()
        )
        for phase in dict.fromkeys(str(value).strip().upper() for value in phases if str(value).strip()):
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        sponsor = str(
            record.payload.get("lead_sponsor")
            or record.payload.get("sponsor")
            or ""
        ).strip()
        if sponsor:
            sponsor_counts[sponsor] = sponsor_counts.get(sponsor, 0) + 1
        for query in _clinical_record_queries(record):
            query_counts[query] = query_counts.get(query, 0) + 1
    relevance_marked = any(
        str(record.payload.get("relevance_status") or "").strip()
        for record in records
    )
    direct_records = tuple(
        record
        for record in records
        if not relevance_marked
        or record.payload.get("relevance_status") == "직접 관련 확인"
    )
    direct_status_counts: dict[str, int] = {}
    direct_phase_counts: dict[str, int] = {}
    direct_sponsor_counts: dict[str, int] = {}
    for record in direct_records:
        status = str(record.payload.get("overall_status") or "").strip().upper()
        status_key = status or "__MISSING__"
        direct_status_counts[status_key] = direct_status_counts.get(status_key, 0) + 1

        raw_phases = record.payload.get("phases")
        phases = (
            (raw_phases,)
            if isinstance(raw_phases, str)
            else raw_phases
            if isinstance(raw_phases, (list, tuple))
            else ()
        )
        normalized_phases = tuple(
            dict.fromkeys(
                str(value).strip().upper() for value in phases if str(value).strip()
            )
        )
        phase_key = " / ".join(normalized_phases) or "__MISSING__"
        direct_phase_counts[phase_key] = direct_phase_counts.get(phase_key, 0) + 1

        sponsor = str(
            record.payload.get("lead_sponsor")
            or record.payload.get("sponsor")
            or ""
        ).strip()
        sponsor_key = sponsor or "__MISSING__"
        direct_sponsor_counts[sponsor_key] = direct_sponsor_counts.get(sponsor_key, 0) + 1

    return {
        "lane": "surface_full_aggregate",
        "records_unique": len(records),
        "status_counts": status_counts,
        "phase_counts": phase_counts,
        "sponsor_counts": sponsor_counts,
        "direct_related_count": len(direct_records),
        "direct_status_counts": direct_status_counts,
        "direct_phase_counts": direct_phase_counts,
        "direct_sponsor_counts": direct_sponsor_counts,
        "query_counts": query_counts,
        **(
            {"temporal_axis": clinical_time_axis(records, observed_on)}
            if observed_on is not None
            else {}
        ),
    }


def _surface_full_aggregate(
    source: str,
    records: Sequence[EvidenceRecord],
    *,
    observed_on: date | None,
) -> dict[str, Any] | None:
    if source == "clinicaltrials":
        return _clinical_full_aggregate(records, observed_on=observed_on)
    if observed_on is None:
        return None
    if source == "patent":
        brand_scope_applied = any(
            record.payload.get("brand_scope_match") in {True, False}
            for record in records
        )
        scoped_records = tuple(
            record
            for record in records
            if not brand_scope_applied
            or record.payload.get("brand_scope_match") is True
        )
        return {
            "lane": "surface_full_temporal",
            "source": source,
            "temporal_axis": patent_time_axis(scoped_records, observed_on),
        }
    if source == "nedrug":
        return {
            "lane": "surface_full_temporal",
            "source": source,
            "temporal_axis": nedrug_time_axis(records, observed_on),
        }
    return None


def _dedupe_mart_render_records(
    records: Sequence[EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    retained: dict[tuple[str, ...], EvidenceRecord] = {}
    for record in records:
        payload = record.payload
        identity = tuple(
            str(payload.get(key) or "").strip()
            for key in (
                "market_id",
                "market_name",
                "view_type",
                "metric",
                "measure",
                "unit_label",
                "brand",
                "period",
                "sales_krw",
                "market_share",
                "prescription_volume",
            )
        )
        current = retained.get(identity)
        if current is None or _mart_record_completeness(
            record
        ) > _mart_record_completeness(current):
            retained[identity] = record
    return tuple(retained.values())


def _mart_record_completeness(record: EvidenceRecord) -> int:
    return sum(
        record.payload.get(key) not in (None, "")
        for key in (
            "rank",
            "market_rank",
            "sales_rank",
            "growth_rate",
            "growth_pct",
            "yoy_growth",
            "yoy_growth_pct",
        )
    )


_TOP_N_RE = re.compile(r"(?:상위|top)\s*\d+", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_KCD_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z]\d{2}(?:\.\d+)?)", re.IGNORECASE)


def _select_hira_records_for_render(
    records: Sequence[EvidenceRecord],
    *,
    queries: Sequence[str],
    question: str,
    limit: int,
) -> tuple[EvidenceRecord, ...]:
    records_by_code: dict[str, list[EvidenceRecord]] = {}
    for record in records:
        code = str(record.payload.get("sickCd") or "").strip().upper()
        if code:
            records_by_code.setdefault(code, []).append(record)

    query_codes = tuple(
        dict.fromkeys(
            match.group(1).upper()
            for query in queries
            for match in _KCD_CODE_RE.finditer(str(query))
        )
    )
    ordered_codes = tuple(
        dict.fromkeys(
            (
                *(code for code in query_codes if code in records_by_code),
                *sorted(code for code in records_by_code if code not in query_codes),
            )
        )
    )
    reserved: list[EvidenceRecord] = []
    for code in ordered_codes[:limit]:
        representative, _rule, _ranked = _select_records_for_question(
            records_by_code[code],
            question=question,
            limit=1,
        )
        reserved.extend(representative)
    reserved_ids = {record.evidence_id for record in reserved}
    slots_left = limit - len(reserved)
    if slots_left <= 0:
        return tuple(reserved)

    matching = tuple(
        record
        for record in records
        if record.evidence_id not in reserved_ids
        if any(
            (
                str(record.payload.get("sickCd") or "").upper() == code
                or str(record.payload.get("sickCd") or "")
                .upper()
                .startswith(f"{code}.")
            )
            for code in query_codes
        )
    )
    matching_ids = {record.evidence_id for record in matching}
    remaining = tuple(
        record
        for record in records
        if record.evidence_id not in reserved_ids
        and record.evidence_id not in matching_ids
    )
    selected_matching, _rule, _ranked = _select_records_for_question(
        matching,
        question=question,
        limit=slots_left,
    )
    selected = [*reserved, *selected_matching]
    slots_left = limit - len(selected)
    if slots_left <= 0:
        return tuple(selected)
    selected_remaining, _rule, _ranked = _select_records_for_question(
        remaining,
        question=question,
        limit=slots_left,
    )
    return (*selected, *selected_remaining)


def _select_records_for_question(
    records: Sequence[EvidenceRecord],
    *,
    question: str,
    limit: int,
) -> tuple[tuple[EvidenceRecord, ...], str, bool]:
    normalized_question = "".join(str(question).casefold().split())
    years = tuple(dict.fromkeys(_YEAR_RE.findall(normalized_question)))
    matching_brands = {
        str(record.payload.get("brand") or "").strip()
        for record in records
        if str(record.payload.get("brand") or "").strip()
        and "".join(str(record.payload.get("brand") or "").casefold().split())
        in normalized_question
    }
    longest_brand_length = max((len(value) for value in matching_brands), default=0)
    target_brands = tuple(
        sorted(
            (value for value in matching_brands if len(value) == longest_brand_length),
            key=str.casefold,
        )
    )
    monthly = "월별" in normalized_question or "monthly" in normalized_question
    requested_metric = _requested_metric(normalized_question)
    has_monthly_records = any(
        re.fullmatch(
            r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])",
            str(record.payload.get("period") or ""),
        )
        is not None
        for record in records
    )

    if monthly and has_monthly_records:
        ordered = sorted(
            records,
            key=lambda record: (
                _brand_penalty(record, target_brands),
                _period_penalty(record, years, require_month=True),
                _metric_penalty(record, requested_metric),
                _descending_period_key(record),
                record.evidence_id,
            ),
        )
        if requested_metric == "sales":
            ordered = list(_unique_market_render_rows(ordered))
        return (
            tuple(ordered[:limit]),
            "question_axis_brand_period_desc_metric_then_evidence_id",
            True,
        )

    has_requested_metric = requested_metric is not None and any(
        _numeric_metric(record, requested_metric) != float("-inf") for record in records
    )
    if _TOP_N_RE.search(normalized_question) and requested_metric and has_requested_metric:
        ordered = sorted(
            records,
            key=lambda record: (
                _brand_penalty(record, target_brands),
                -_numeric_metric(record, requested_metric),
                record.evidence_id,
            ),
        )
        return (
            tuple(ordered[:limit]),
            "question_axis_metric_desc_then_evidence_id",
            True,
        )

    if requested_metric and has_requested_metric:
        ordered = sorted(
            records,
            key=lambda record: (
                _renderable_metric_penalty(record, requested_metric),
                _metric_penalty(record, requested_metric),
                _brand_penalty(record, target_brands),
                _descending_period_key(record),
                record.evidence_id,
            ),
        )
        if requested_metric == "sales":
            ordered = list(_unique_market_render_rows(ordered))
        return (
            tuple(ordered[:limit]),
            "question_axis_renderable_metric_period_desc_then_evidence_id",
            True,
        )

    if target_brands:
        ordered = sorted(
            records,
            key=lambda record: (_brand_penalty(record, target_brands), record.evidence_id),
        )
        return tuple(ordered[:limit]), "question_axis_brand_then_evidence_id", True

    ordered = sorted(records, key=lambda record: record.evidence_id)
    return tuple(ordered[:limit]), "stable_evidence_id_order", False


def _requested_metric(question: str) -> str | None:
    if "매출" in question or "sales" in question:
        return "sales"
    if "점유율" in question or "share" in question:
        return "market_share"
    if "환자수" in question or "patient" in question:
        return "patient_count"
    if "처방" in question or "prescription" in question:
        return "prescription_volume"
    return None


def _brand_penalty(record: EvidenceRecord, target_brands: Sequence[str]) -> int:
    if not target_brands:
        return 0
    brand = "".join(str(record.payload.get("brand") or "").casefold().split())
    return (
        0
        if any(brand == "".join(value.casefold().split()) for value in target_brands)
        else 1
    )


def _period_penalty(
    record: EvidenceRecord,
    years: Sequence[str],
    *,
    require_month: bool,
) -> int:
    period = str(record.payload.get("period") or "")
    if (
        require_month
        and re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", period) is None
    ):
        return 2
    if years and not any(period.startswith(year) for year in years):
        return 1
    return 0


def _descending_period_key(record: EvidenceRecord) -> tuple[int, int, int, str]:
    period = str(record.payload.get("period") or "").strip()
    match = re.fullmatch(r"((?:19|20)\d{2})(?:-(0[1-9]|1[0-2]))?", period)
    if match is None:
        return (1, 0, 0, period)
    return (0, -int(match.group(1)), -int(match.group(2) or 0), period)


def _metric_penalty(record: EvidenceRecord, requested_metric: str | None) -> int:
    if requested_metric is None:
        return 0
    payload = record.payload
    metric = str(payload.get("metric") or "").casefold()
    aliases = {
        "sales": ("sales", "sales_krw", "value_억원"),
        "market_share": ("market_share", "share", "ms_pct"),
        "patient_count": ("patient_count", "patients"),
        "prescription_volume": ("prescription_volume", "volume", "rx"),
    }
    if metric in aliases.get(requested_metric, ()):
        return 0
    return (
        0
        if any(payload.get(key) is not None for key in aliases.get(requested_metric, ()))
        else 1
    )


def _renderable_metric_penalty(
    record: EvidenceRecord,
    requested_metric: str | None,
) -> int:
    if requested_metric != "sales":
        return 0
    return 0 if record.payload.get("sales_krw") not in (None, "") else 1


def _unique_market_render_rows(
    records: Sequence[EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    retained: dict[tuple[str, ...], EvidenceRecord] = {}
    growth_keys = ("growth_rate", "growth_pct", "yoy_growth", "yoy_growth_pct")
    for record in records:
        payload = record.payload
        growth = next(
            (payload.get(key) for key in growth_keys if payload.get(key) not in (None, "")),
            None,
        )
        identity = tuple(
            str(value or "").strip().casefold()
            for value in (
                payload.get("market_id") or payload.get("market_name"),
                payload.get("brand"),
                payload.get("period"),
                payload.get("sales_krw"),
                payload.get("market_share"),
                growth,
            )
        )
        retained.setdefault(identity, record)
    return tuple(retained.values())


def _numeric_metric(record: EvidenceRecord, requested_metric: str) -> float:
    keys = {
        "sales": ("sales_krw", "value_억원", "value"),
        "market_share": ("market_share", "ms_pct", "share"),
        "patient_count": ("patient_count", "patients", "value"),
        "prescription_volume": ("prescription_volume", "volume", "value"),
    }.get(requested_metric, ())
    for key in keys:
        value = record.payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return float("-inf")


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _prompt_trace(before: int, after: int, applied: bool, strategy: str) -> dict[str, Any]:
    return {
        "applied": applied,
        "before_chars": before,
        "after_chars": after,
        "strategy": strategy,
        "records_discarded": 0,
        "inspection_retains_full_payload": True,
    }


_ID_KEYS = (
    "evidence_id", "record_id", "id", "nct_id", "nctId", "NCTId",
    "patent_no", "patentNumber", "item_seq", "ITEM_SEQ", "brand", "title",
)
_VERBOSE_KEYS = frozenset({
    "raw_text", "description", "brief_summary", "detailed_description",
    "eligibilityCriteria", "locations", "source_record", "official_text",
})


def _compact_value(value: Any, *, aggressive: bool) -> Any:
    if isinstance(value, Mapping):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in _VERBOSE_KEYS:
                if not aggressive and item:
                    compacted[f"{key}_summary"] = f"present:{len(str(item))}chars"
                continue
            compacted[str(key)] = _compact_value(item, aggressive=aggressive)
        return compacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        if aggressive and len(items) > 12 and all(isinstance(item, Mapping) for item in items):
            return {
                "record_count": len(items),
                "record_identifiers": [
                    _record_identifier(item) for item in items if _record_identifier(item)
                ],
            }
        return [_compact_value(item, aggressive=aggressive) for item in items]
    if isinstance(value, str):
        limit = 180 if aggressive else 600
        return value if len(value) <= limit else f"{value[:limit]}…[{len(value)}chars]"
    return value


def _record_identifier(record: Mapping[str, Any]) -> str | None:
    for key in _ID_KEYS:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    for value in record.values():
        if isinstance(value, Mapping):
            nested = _record_identifier(value)
            if nested:
                return nested
    return None


def _identifier_manifest(messages: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    identifiers: list[str] = []
    for message in messages:
        try:
            value = json.loads(message.get("content", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        _collect_identifiers(value, identifiers)
    unique_identifiers = list(dict.fromkeys(identifiers))
    return {
        "prompt_compacted": True,
        "record_count": len(unique_identifiers),
        "record_identifiers": unique_identifiers,
        "instruction": "Full records remain in inspection detail; synthesize only from this manifest.",
    }


def _collect_identifiers(value: Any, output: list[str]) -> None:
    if isinstance(value, Mapping):
        identifier = _record_identifier(value)
        if identifier:
            output.append(identifier)
        for item in value.values():
            _collect_identifiers(item, output)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_identifiers(item, output)
