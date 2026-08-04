from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
import json
import re

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ToolFailureRecord,
    V3EvidenceBundle,
    V3EvidenceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion_limitations import (
    deferred_limitation,
    failure_limitation,
    failure_reason_code,
)


_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
)
_PERIOD_VALUE = re.compile(
    r"^\d{4}(?:-(?:Q[1-4]|\d{1,2})(?:-\d{1,2})?)?$",
    re.IGNORECASE,
)
_STRUCTURAL_NUMERIC_KEYS = frozenset(
    {
        "arguments",
        "atc4",
        "code",
        "evidence_id",
        "file_id",
        "id",
        "market_id",
        "month",
        "period",
        "projection_missing_reasons",
        "projection_sources",
        "quarter",
        "source",
        "tool_name",
        "view",
        "workflow_id",
        "year",
    }
)
_SYSTEM_PROMPT = """You produce a Korean evidence-bound answer as one JSON object.
The only allowed shape is {"claims":[{"text":str,"evidence_ids":[str]}],"limitations":[str]}.
Every claim must cite one or more supplied evidence_id values.
Copy numeric literals exactly from allowed_numeric_literals of the cited evidence; never calculate, estimate, round, interpolate, or convert units.
Copy periods from allowed_periods exactly, or use their direct Korean year/month/quarter notation.
Use only supplied evidence values. Write natural Korean around them without changing values.
Keep member_population (the full mart-observed universe), active_members (positive value in a named period), and display_members (the UI projection) distinct. State the layer whenever describing brand counts or lists.
Every HHI claim must state its supplied period. Do not combine market size and HHI from different periods in one claim.
When some evidence is unavailable, keep claims supported by successful evidence and include every supplied failure limitation.
When no evidence supports a claim, return no claim for that facet. Do not expose internal errors, implementation details, or hidden reasons.
Return JSON only."""


def build_fusion_messages(
    question: str,
    bundle: V3EvidenceBundle,
) -> list[dict[str, str]]:
    evidence = [fusion_fact_payload(fact) for fact in bundle.facts]
    failures = [
        {
            "reason_code": reason_code,
            "limitation": failure_limitation(failure, reason_code=reason_code),
        }
        for failure in bundle.failures
        for reason_code in (failure_reason_code(failure),)
    ]
    failures.extend(
        {
            "reason_code": "deferred_evidence",
            "limitation": deferred_limitation(deferred),
        }
        for deferred in bundle.deferred
    )
    payload = {"question": question, "evidence": evidence, "failures": failures}
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    ]


def fusion_fact_payload(fact: V3EvidenceFact) -> dict[str, object]:
    values = object_mapping(fact)
    canonical = {
        key: prompt_value(values.get(key))
        for key in (
            "entity",
            "metric",
            "period",
            "unit",
            "view",
            "market",
            "effective_date",
            "last_checked",
            "status",
            "last_update_posted",
            "file_id",
            "sheet",
            "range",
        )
        if key in values and values.get(key) is not None
    }
    arguments = prompt_value(fact.arguments)
    raw_result = prompt_value(fact.raw_result)
    return {
        "evidence_id": fact.evidence_id,
        "fact_type": fact.fact_type,
        "tool_name": fact.tool_name,
        "arguments": arguments,
        "canonical": canonical,
        "raw_result": raw_result,
        "missing_required_fields": list(fact.missing_required_fields),
        "allowed_numeric_literals": sorted(fact_numeric_literals(fact)),
        "allowed_periods": sorted(fact_period_literals(fact)),
    }


def numeric_literals(text: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _NUMBER.finditer(text))


def numeric_literal_spans(text: str) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (match.group(0), match.start(), match.end()) for match in _NUMBER.finditer(text)
    )


def canonical_numeric_literal(value: str) -> str:
    return value.replace(",", "").lstrip("+")


def fact_numeric_literals(fact: V3EvidenceFact) -> frozenset[str]:
    values: set[str] = set()
    _collect_semantic_numeric_values(fact.raw_result, values)
    return frozenset(values)


def fact_period_literals(fact: V3EvidenceFact) -> frozenset[str]:
    values: set[str] = set()
    for field in ("period", "effective_date", "last_checked", "last_update_posted"):
        _collect_period_values(getattr(fact, field, None), values)
    _collect_period_values(fact.raw_result, values)
    return frozenset(values)


def message_numeric_literals(failure: ToolFailureRecord) -> frozenset[str]:
    return frozenset(
        canonical_numeric_literal(value) for value in numeric_literals(failure.message)
    )


def _collect_semantic_numeric_values(
    value: object,
    output: set[str],
    *,
    field_name: str | None = None,
) -> None:
    if _is_structural_numeric_field(field_name):
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float):
        output.add(canonical_numeric_literal(str(value)))
        return
    if isinstance(value, str):
        output.update(
            canonical_numeric_literal(literal) for literal in numeric_literals(value)
        )
        return
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect_semantic_numeric_values(item, output, field_name=str(key))
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_semantic_numeric_values(item, output, field_name=field_name)


def _collect_period_values(value: object, output: set[str]) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        stripped = value.strip()
        if _PERIOD_VALUE.fullmatch(stripped) is not None:
            output.add(stripped)
        return
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect_period_values(str(key), output)
            _collect_period_values(item, output)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_period_values(item, output)


def _is_structural_numeric_field(field_name: str | None) -> bool:
    if field_name is None:
        return False
    normalized = field_name.casefold()
    return (
        normalized in _STRUCTURAL_NUMERIC_KEYS
        or normalized.endswith(("_id", "_code", "_period", "_year", "_month", "_quarter", "_at"))
    )


def object_mapping(value: object) -> Mapping[str, object]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def prompt_value(value: object, *, depth: int = 0) -> object:
    if depth >= 8:
        return "<nested value omitted>"
    if is_dataclass(value) and not isinstance(value, type):
        return prompt_value(asdict(value), depth=depth + 1)
    if hasattr(value, "model_dump"):
        return prompt_value(value.model_dump(), depth=depth + 1)
    if isinstance(value, Mapping):
        return {
            str(key): prompt_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [prompt_value(item, depth=depth + 1) for item in list(value)[:40]]
        if len(value) > 40:
            items.append("<additional items omitted>")
        return items
    if isinstance(value, bytes):
        return "<binary value omitted>"
    if isinstance(value, str):
        return value if len(value) <= 4000 else f"{value[:4000]}<text omitted>"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


__all__ = [
    "build_fusion_messages",
    "canonical_numeric_literal",
    "fact_numeric_literals",
    "fact_period_literals",
    "fusion_fact_payload",
    "message_numeric_literals",
    "numeric_literal_spans",
    "numeric_literals",
]
