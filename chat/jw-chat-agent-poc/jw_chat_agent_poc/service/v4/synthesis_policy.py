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

    return bounded, _prompt_trace(before, after, True, strategy)


def prune_unsupported_source_queries(
    plan: PlannerOutput,
) -> tuple[PlannerOutput, dict[str, Any]]:
    if os.environ.get("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", "1").strip().casefold() in {
        "0",
        "false",
        "off",
        "no",
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
            coverage = evidence.coverage.model_copy(
                update={
                    "partial_reasons": tuple(
                        dict.fromkeys(
                            (*evidence.coverage.partial_reasons, "surface_render_limit")
                        )
                    )
                }
            )
            evidence = evidence.model_copy(
                update={"records": evidence.records[:shown], "coverage": coverage}
            )
        limited.append(evidence)
    return tuple(limited), {"applied": bool(sources), "sources": sources}


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
