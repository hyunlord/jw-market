from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass, replace
import hashlib
import json
import re

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ClinicalTrialFact,
    RegulatoryRuleFact,
    ToolFailureRecord,
    V3EvidenceBundle,
    V3EvidenceFact,
    WebSourceFact,
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
_WEB_EXCERPT_MAX_CHARS = 1200
_REDUNDANT_MARKET_RENDER_TOOLS = frozenset(
    {
        "market.compare_brands",
        "market.get_brand_metric",
        "market.get_growth_contribution",
        "market.get_hhi",
        "market.get_timeseries",
    }
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
evidence_id는 supplied evidence 목록의 값을 그대로 복사하고 새로 만들지 않는다.
한글 필드명, 결과 순번, NCT 식별자를 evidence_id로 조합하지 않는다.
인용할 supplied evidence_id가 없으면 claim을 만들지 말고 limitations에 남긴다.
Copy numeric literals exactly from allowed_numeric_literals of the cited evidence; never calculate, estimate, round, interpolate, or convert units.
Copy periods from allowed_periods exactly, or use their direct Korean year/month/quarter notation.
Use only supplied evidence values. Write natural Korean around them without changing values.
Keep member_population (the full mart-observed universe), active_members (positive value in a named period), and display_members (the UI projection) distinct. State the layer whenever describing brand counts or lists.
Every HHI claim must state its supplied period. Do not combine market size and HHI from different periods in one claim.
For web_source evidence, quote only the supplied excerpt, visibly include its exact URL in the claim, and describe it as an external source rather than internal data.
Web numeric literals come only from web_quoted_numeric_literals and remain supplementary; they are never internal calculated values.
When web evidence declares conflicts_with_evidence_ids, cite both the web and internal evidence, state both values without averaging, and add a limitation that identifies the difference.
When some evidence is unavailable, keep claims supported by successful evidence and include every supplied failure limitation.
When no evidence supports a claim, return no claim for that facet. Do not expose internal errors, implementation details, or hidden reasons.
Return JSON only."""


def build_fusion_messages(
    question: str,
    bundle: V3EvidenceBundle,
) -> list[dict[str, str]]:
    evidence = [fusion_fact_payload(fact) for fact in fusion_citation_facts(bundle.facts)]
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
    if isinstance(fact, WebSourceFact):
        values["excerpt"] = fusion_web_excerpt(fact)
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
            "url",
            "title",
            "excerpt",
            "fetched_at_utc",
            "domain",
            "search_query",
            "result_rank",
            "source_grade",
            "search_stage",
            "conflicts_with_evidence_ids",
        )
        if key in values and values.get(key) is not None
    }
    arguments = prompt_value(fact.arguments)
    raw_result_value = _fusion_prompt_raw_result(fact)
    if isinstance(fact, WebSourceFact):
        raw_result_value = {
            key: value
            for key, value in raw_result_value.items()
            if key not in {"snippet", "content", "raw_content"}
        }
    raw_result = prompt_value(raw_result_value)
    return {
        "evidence_id": fact.evidence_id,
        "fact_type": fact.fact_type,
        "tool_name": fact.tool_name,
        "arguments": arguments,
        "canonical": canonical,
        "raw_result": raw_result,
        "missing_required_fields": list(fact.missing_required_fields),
        "allowed_numeric_literals": (
            [] if isinstance(fact, WebSourceFact) else sorted(fact_numeric_literals(fact))
        ),
        "web_quoted_numeric_literals": (
            sorted(web_source_numeric_literals(fact))
            if isinstance(fact, WebSourceFact)
            else []
        ),
        "allowed_periods": sorted(fact_period_literals(fact)),
    }


def fusion_citation_facts(
    facts: Sequence[V3EvidenceFact],
) -> tuple[V3EvidenceFact, ...]:
    expanded: list[V3EvidenceFact] = []
    for fact in facts:
        expanded.append(fact)
        for item in _nested_evidence_items(fact.raw_result):
            expanded.append(_citation_item_fact(fact, item))
    return tuple(expanded)


def _nested_evidence_items(raw_result: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(raw_result, Mapping):
        return ()
    evidence = raw_result.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in evidence if isinstance(item, Mapping))


def _citation_item_fact(
    parent: V3EvidenceFact,
    item: Mapping[str, object],
) -> V3EvidenceFact:
    public_item = {
        str(key): value
        for key, value in item.items()
        if str(key) not in {"fact_id", "raw_ref"}
    }
    identity = json.dumps(
        {
            "parent_evidence_id": parent.evidence_id,
            "item": prompt_value(item),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    evidence_id = (
        f"v3-shadow:{parent.tool_name}:"
        f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
    )
    common = {
        "evidence_id": evidence_id,
        "raw_result": public_item,
        "missing_required_fields": (),
        "projection_sources": (),
        "projection_missing_reasons": (),
    }
    if isinstance(parent, ClinicalTrialFact):
        return replace(parent, status=None, last_update_posted=None, **common)
    if isinstance(parent, RegulatoryRuleFact):
        return replace(parent, effective_date=None, last_checked=None, **common)
    return replace(parent, **common)


def _without_nested_evidence(raw_result: object) -> object:
    if not isinstance(raw_result, Mapping) or not isinstance(raw_result.get("evidence"), list):
        return raw_result
    return {key: value for key, value in raw_result.items() if key != "evidence"}


def _fusion_prompt_raw_result(fact: V3EvidenceFact) -> object:
    raw_result = _without_nested_evidence(fact.raw_result)
    if fact.tool_name not in _REDUNDANT_MARKET_RENDER_TOOLS:
        return raw_result
    if not isinstance(raw_result, Mapping):
        return raw_result
    render_data = raw_result.get("render_data")
    if not isinstance(render_data, Mapping):
        return raw_result
    projected_render = {
        str(key): _market_render_prompt_value(str(key), value)
        for key, value in render_data.items()
    }
    return {**raw_result, "render_data": projected_render}


def _market_render_prompt_value(key: str, value: object) -> object:
    if key == "level_segments":
        return _compact_market_rows(value, redundant_keys=frozenset({"name"}))
    if key != "level_top5_trend_series":
        return value
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return value
    return [
        {
            str(item_key): (
                _compact_market_rows(
                    item_value,
                    redundant_keys=frozenset({"value"}),
                )
                if str(item_key) == "series"
                else item_value
            )
            for item_key, item_value in item.items()
        }
        if isinstance(item, Mapping)
        else item
        for item in value
    ]


def _compact_market_rows(
    value: object,
    *,
    redundant_keys: frozenset[str],
) -> object:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return value
    compact_rows = [
        {
            str(item_key): item_value
            for item_key, item_value in item.items()
            if str(item_key) not in redundant_keys
        }
        if isinstance(item, Mapping)
        else item
        for item in value
    ]
    if len(compact_rows) < 2 or not all(isinstance(item, Mapping) for item in compact_rows):
        return compact_rows
    columns = list(
        dict.fromkeys(
            key
            for item in compact_rows
            for key in item
        )
    )
    constants = {
        key: compact_rows[0].get(key)
        for key in columns
        if all(item.get(key) == compact_rows[0].get(key) for item in compact_rows[1:])
    }
    variable_columns = [key for key in columns if key not in constants]
    return {
        "constant_fields": constants,
        "columns": variable_columns,
        "rows": [
            [item.get(key) for key in variable_columns]
            for item in compact_rows
        ],
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
    if isinstance(fact, WebSourceFact):
        return frozenset()
    values: set[str] = set()
    _collect_semantic_numeric_values(fact.raw_result, values)
    return frozenset(values)


def web_source_numeric_literals(fact: WebSourceFact) -> frozenset[str]:
    return frozenset(
        canonical_numeric_literal(value) for value in numeric_literals(fusion_web_excerpt(fact))
    )


def fusion_web_excerpt(fact: WebSourceFact) -> str:
    return fact.excerpt[:_WEB_EXCERPT_MAX_CHARS]


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
    "fusion_citation_facts",
    "fusion_fact_payload",
    "message_numeric_literals",
    "numeric_literal_spans",
    "numeric_literals",
]
