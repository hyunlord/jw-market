from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import ValidationError

from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact
from jw_chat_agent_poc.tool_use.renderer import render_evidence_claim

_AUTHORITATIVE_EXTERNAL_PREFIXES: Final[tuple[str, ...]] = (
    "clinicaltrials_",
    "mfds_",
)
_CLINICAL_MISSING_VALUE_PREFIX: Final[str] = "ClinicalTrials 상세 응답에서"


def project_authoritative_external_evidence(
    tool_calls: Sequence[Mapping[str, Any]],
    fact_md: str,
) -> list[dict[str, Any]]:
    """Project rendered MFDS/ClinicalTrials facts into the legacy evidence schema."""

    rendered_lines = frozenset(fact_md.splitlines())
    projected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for call in tool_calls:
        tool = str(call.get("tool") or "")
        if not tool.startswith(_AUTHORITATIVE_EXTERNAL_PREFIXES):
            continue
        if str(call.get("status") or "") != "ok":
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping) or render_data.get("ok") is False:
            continue
        serialized = render_data.get("evidence")
        if not isinstance(serialized, Sequence) or isinstance(serialized, (str, bytes)):
            continue
        for raw_fact in serialized:
            if not isinstance(raw_fact, Mapping):
                continue
            try:
                fact = EvidenceFact.model_validate(raw_fact)
            except ValidationError:
                continue
            rendered = render_evidence_claim(fact)
            if (
                fact.fact_id in seen_ids
                or rendered not in rendered_lines
                or not _has_actual_value(tool, fact)
            ):
                continue
            seen_ids.add(fact.fact_id)
            projected.append(_legacy_fact(tool, fact, rendered))
    return projected


def _has_actual_value(tool: str, fact: EvidenceFact) -> bool:
    if fact.value is not None:
        return True
    locator = (fact.source_locator or "").strip()
    if not locator:
        return False
    return not (
        tool.startswith("clinicaltrials_")
        and locator.startswith(_CLINICAL_MISSING_VALUE_PREFIX)
    )


def _legacy_fact(
    tool: str,
    fact: EvidenceFact,
    rendered: str,
) -> dict[str, Any]:
    value = (
        f"{fact.value}{fact.unit or ''}"
        if fact.value is not None
        else str(fact.source_locator or "")
    )
    return {
        "fact_id": fact.fact_id,
        "label": fact.metric,
        "value": value,
        "source": fact.source_name,
        "tool": tool,
        "path": fact.raw_ref or f"render_data.evidence.{fact.fact_id}",
        "period": fact.period or "",
        "allowed_numbers": list(allowed_numbers(rendered)),
        "visible": True,
        "entity": fact.subject,
        "metric": fact.metric,
        "unit": fact.unit or "",
        "source_grade": "AUTHORITATIVE",
        "view": "",
        "operand_fact_ids": [],
    }
