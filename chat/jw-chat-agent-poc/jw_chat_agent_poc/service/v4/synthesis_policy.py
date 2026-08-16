from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, QueryScope, SourceName
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet


# Defaults come from the 2026-08-15 live sample: request p95 125.9s/max 162.9s,
# successful synthesis max 69.5s, and large valid CT prompts around 85-149k chars.
_DEFAULT_TOTAL_REQUEST_BUDGET_S = 180.0
_DEFAULT_MAX_SYNTHESIS_BUDGET_S = 75.0
_DEFAULT_MIN_SYNTHESIS_BUDGET_S = 15.0
_DEFAULT_PROMPT_CHAR_LIMIT = 120_000
_DEFAULT_SOURCE_RENDER_LIMIT = 40


@dataclass(frozen=True)
class SynthesisPolicy:
    total_request_budget_s: float
    max_synthesis_budget_s: float
    min_synthesis_budget_s: float
    prompt_char_limit: int
    source_render_limit: int

    @classmethod
    def from_env(cls) -> "SynthesisPolicy":
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
                ),
            }
        ),
        {"applied": True, "omitted": omitted_trace},
    )


def limit_evidence_sets_for_render(
    evidence_sets: Sequence[EvidenceSet],
    *,
    per_source_limit: int,
) -> tuple[tuple[EvidenceSet, ...], dict[str, Any]]:
    if per_source_limit <= 0:
        raise ValueError("per_source_limit must be positive")
    limited: list[EvidenceSet] = []
    sources: dict[str, dict[str, int]] = {}
    for evidence in evidence_sets:
        total = len(evidence.records)
        shown = min(total, per_source_limit)
        if shown < total:
            sources[evidence.source] = {"shown": shown, "total": total}
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
                for record in evidence.records[:shown]
                for ref in record.source_refs
            }
            attributable = {
                ref.url for record in evidence.records for ref in record.source_refs
            }
            coverage = evidence.coverage.model_copy(
                update={
                    "partial_reasons": tuple(
                        dict.fromkeys(
                            (*evidence.coverage.partial_reasons, "surface_render_limit")
                        )
                    )
                }
            )
            retained_refs = tuple(
                ref
                for ref in evidence.source_refs
                if ref.url not in attributable or ref.url in kept_refs
            )
            sources[evidence.source]["refs_shown"] = len(retained_refs)
            sources[evidence.source]["refs_total"] = len(evidence.source_refs)
            evidence = evidence.model_copy(
                update={
                    "records": evidence.records[:shown],
                    "coverage": coverage,
                    "source_refs": retained_refs,
                }
            )
        limited.append(evidence)
    return tuple(limited), {
        "applied": bool(sources),
        "sources": sources,
        # The kept records are the first N in upstream return order — no relevance
        # ranking runs here. Naming that lets the surface say so instead of letting
        # "40 표시" read as "the 40 best".
        "selection_rule": "leading_records_in_upstream_order",
        "selection_is_ranked": False,
    }


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
