from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.tool_use.routing_v4_types import (
    ExecutedCallSignature,
    ProposedRoutingSignature,
)


_UNKNOWN = "unknown"


def project_routing_v4_qa_trace(
    diagnostics: Mapping[str, Any],
) -> dict[str, Any] | None:
    raw = diagnostics.get("routing_v4")
    if not isinstance(raw, Mapping):
        return None

    projected: dict[str, Any] = {
        "routing_mode": str(raw.get("routing_mode") or _UNKNOWN),
        "eligible_tools": tuple(str(item) for item in raw.get("eligible_tools", ())),
        "reason_code": raw.get("reason_code"),
        "repair_count": int(raw.get("repair_count") or 0),
        "deterministic_rule_id": raw.get("deterministic_rule_id"),
    }
    if raw.get("shadow_status") is not None:
        projected["shadow_status"] = str(raw["shadow_status"])
    invariant = raw.get("legacy_response_invariant")
    if isinstance(invariant, Mapping):
        projected["legacy_response_invariant"] = {
            "before_sha256": str(invariant.get("before_sha256") or _UNKNOWN),
            "after_sha256": str(invariant.get("after_sha256") or _UNKNOWN),
            "unchanged": invariant.get("unchanged") is True,
        }

    proposed = raw.get("proposed_routing_signature")
    if isinstance(proposed, Mapping):
        try:
            projected["proposed_routing_signature"] = ProposedRoutingSignature.model_validate(
                proposed
            ).model_dump(mode="json")
        except ValueError:
            projected["proposed_routing_signature_status"] = "invalid"

    executed = raw.get("executed_call_signature")
    if isinstance(executed, Mapping):
        try:
            projected["executed_call_signature"] = ExecutedCallSignature.model_validate(
                executed
            ).model_dump(mode="json")
        except ValueError:
            projected["executed_call_signature_status"] = "invalid"

    if raw.get("claim_evidence_binding_status") is not None:
        projected["claim_evidence_binding_status"] = str(
            raw["claim_evidence_binding_status"]
        )
        projected["claim_evidence_bindings"] = _project_evidence_bindings(
            raw.get("claim_evidence_bindings")
        )
    return projected


def _project_evidence_bindings(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    bindings: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        raw_evidence_ids = item.get("evidence_ids")
        evidence_ids = (
            tuple(str(evidence_id) for evidence_id in raw_evidence_ids)
            if isinstance(raw_evidence_ids, Sequence)
            and not isinstance(raw_evidence_ids, str | bytes)
            else ()
        )
        bindings.append(
            {
                "claim_ordinal": int(item.get("claim_ordinal") or 0),
                "tool_name": str(item.get("tool_name") or _UNKNOWN),
                "evidence_ids": evidence_ids,
            }
        )
    return tuple(bindings)
