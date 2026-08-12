from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.evidence_payload import is_request_metadata_key
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.markdown_fences import advance_fence_state


ClaimType = Literal["T1", "T2", "T3"]
CausalLevel = Literal["NONE", "TEMPORAL", "ASSOCIATION", "CAUSAL"]
Modality = Literal["ASSERTED", "OBSERVED", "NOT_ESTABLISHED"]
_SENTENCE_RE = re.compile(r"(?<=[.!?。])\s+(?=[^\s])")
_HIGH_ENTROPY_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])NCT\d{8}(?!\d)|"
    r"(?<![A-Za-z0-9])[A-Z]{2,}[A-Z0-9]*-\d+[A-Za-z]?(?![A-Za-z0-9])|"
    r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|"
    r"(?<![A-Za-z0-9.,])[-+]?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z0-9.,]))",
    re.IGNORECASE,
)
_COMPANY_RE = re.compile(
    r"(?:\b[A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*)*\s+"
    r"(?:Pharmaceuticals?|Pharma|Biopharma|Biotech|Corporation|Corp|Company|Inc|Ltd)"
    r"|[가-힣A-Za-z0-9]{2,}(?:제약|바이오|약품|헬스케어))"
)
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CAUSAL_RE = re.compile(r"(?:때문|원인|야기|일으켰|영향을\s*줬|caus)", re.IGNORECASE)
_CURRENCY_CODE_RE = re.compile(
    r"(?:^|_)([a-z]{3})(?=_|$)",
    re.IGNORECASE,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimArgument(_FrozenModel):
    record_id: str
    field_path: str
    value_hash: str


class ClaimIR(_FrozenModel):
    claim_id: str
    claim_type: ClaimType
    predicate_id: str
    arguments: tuple[ClaimArgument, ...]
    support_set_id: str
    operator_id: str
    rule_id: str | None = None
    entity_scope: tuple[str, ...] = ()
    time_scope: tuple[str, ...] = ()
    causal_level: CausalLevel = "NONE"
    modality: Modality = "NOT_ESTABLISHED"
    schema_version: Literal["1.0"] = "1.0"


class ClaimClassificationResult(_FrozenModel):
    answer: str
    answer_mutation: bool
    claim_ir: tuple[ClaimIR, ...]
    recomputation_evidence: tuple[dict[str, Any], ...] = ()
    density_metrics: dict[str, Any]


def classify_answer_claims(
    answer: str,
    evidence_sets: Sequence[EvidenceSet],
) -> ClaimClassificationResult:
    records = tuple(record for item in evidence_sets for record in item.records)
    fields = {record.evidence_id: _flatten_record(record) for record in records}
    claims: list[ClaimIR] = []
    recomputations: list[dict[str, Any]] = []
    supported_record_ids: set[str] = set()
    supported_high_entropy = _supported_high_entropy_tokens(fields)
    supported_field_values = frozenset(
        value.casefold() for value in _all_field_values(fields)
    )
    for index, sentence in enumerate(_sentences(answer), start=1):
        arguments = _arguments_for_sentence(sentence, fields)
        explicit_record_ids = tuple(
            record.evidence_id
            for record in records
            if _record_identity(record).casefold() in sentence.casefold()
        )
        if explicit_record_ids:
            arguments = tuple(
                argument
                for argument in arguments
                if argument.record_id in explicit_record_ids
            )
        record_ids = tuple(dict.fromkeys(argument.record_id for argument in arguments))
        unsupported_high_entropy = tuple(
            token
            for token in _HIGH_ENTROPY_RE.findall(sentence)
            if token.casefold() not in supported_high_entropy
        )
        unsupported_companies = tuple(
            company
            for company in _COMPANY_RE.findall(sentence)
            if company.casefold() not in supported_field_values
        )
        relation = _recomputed_relation(sentence, arguments, fields)
        has_unsupported_exact_value = bool(
            unsupported_high_entropy or unsupported_companies
        )
        if len(record_ids) == 1 and arguments and not has_unsupported_exact_value:
            claim_type: ClaimType = "T1"
            operator_id = "field_restatement"
            predicate_id = "field_restatement"
            modality: Modality = "OBSERVED"
        elif (
            len(record_ids) >= 2
            and relation is not None
            and not has_unsupported_exact_value
        ):
            claim_type = "T2"
            operator_id, recomputed = relation
            predicate_id = operator_id
            modality = "OBSERVED"
            recomputations.append(
                {
                    "claim_index": index,
                    "operator_id": operator_id,
                    "record_ids": list(record_ids),
                    "input_record_count": len(record_ids),
                    "result": recomputed,
                }
            )
        else:
            claim_type = "T3"
            operator_id = "inference_unvalidated"
            predicate_id = "causal_inference" if _CAUSAL_RE.search(sentence) else "inference"
            modality = "NOT_ESTABLISHED"
        support_set_id = _stable_id("SUPPORT", record_ids)
        sentence_hash = sha256(sentence.encode("utf-8")).hexdigest()
        claim = ClaimIR(
            claim_id=f"CLAIM-{index:04d}-{sentence_hash[:12]}",
            claim_type=claim_type,
            predicate_id=predicate_id,
            arguments=arguments,
            support_set_id=support_set_id,
            operator_id=operator_id,
            entity_scope=record_ids,
            time_scope=tuple(
                dict.fromkeys(
                    match.group(0)
                    for match in re.finditer(
                        r"(?<!\d)\d{4}(?:-\d{2}-\d{2})?(?!\d)",
                        sentence,
                    )
                )
            ),
            causal_level="CAUSAL" if _CAUSAL_RE.search(sentence) else "NONE",
            modality=modality,
        )
        claims.append(claim)
        if claim_type in {"T1", "T2"}:
            supported_record_ids.update(record_ids)
    metrics = _density_metrics(answer, claims, records, fields, supported_record_ids)
    return ClaimClassificationResult(
        answer=answer,
        answer_mutation=False,
        claim_ir=tuple(claims),
        recomputation_evidence=tuple(recomputations),
        density_metrics=metrics,
    )


def _sentences(answer: str) -> tuple[str, ...]:
    sentences: list[str] = []
    fence_state = None
    for line in answer.splitlines():
        fence_state, is_fence_boundary = advance_fence_state(fence_state, line)
        if is_fence_boundary or fence_state is not None:
            continue
        if line.strip() and not line.lstrip().startswith(("#", "|")):
            prose = line.strip(" -*>")
            sentences.extend(
                item.strip() for item in _SENTENCE_RE.split(prose) if item.strip()
            )
    return tuple(sentences)


def _arguments_for_sentence(
    sentence: str,
    fields: Mapping[str, Mapping[str, str]],
) -> tuple[ClaimArgument, ...]:
    arguments: list[ClaimArgument] = []
    lowered = sentence.casefold()
    for record_id, record_fields in fields.items():
        for field_path, value in record_fields.items():
            if len(value) < 2 or value.casefold() not in lowered:
                continue
            arguments.append(
                ClaimArgument(
                    record_id=record_id,
                    field_path=field_path,
                    value_hash=sha256(value.encode("utf-8")).hexdigest(),
                )
            )
    return tuple(arguments)


def _flatten_record(record: EvidenceRecord) -> dict[str, str]:
    flattened: dict[str, str] = {"evidence_id": record.evidence_id}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if is_request_metadata_key(str(key)):
                    continue
                walk(nested, f"{path}.{key}" if path else str(key))
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")
        elif value not in (None, ""):
            flattened[path] = str(value)

    walk(record.payload, "payload")
    return flattened


def _record_identity(record: EvidenceRecord) -> str:
    for key in ("nct_id", "patent_number", "application_number", "record_id"):
        value = record.payload.get(key)
        if value not in (None, ""):
            return str(value)
    return record.evidence_id.rsplit(":", 1)[-1]


def _all_field_values(fields: Mapping[str, Mapping[str, str]]) -> tuple[str, ...]:
    return tuple(value for record_fields in fields.values() for value in record_fields.values())


def _supported_high_entropy_tokens(
    fields: Mapping[str, Mapping[str, str]],
) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for value in _all_field_values(fields)
        for token in _HIGH_ENTROPY_RE.findall(value)
    )


def _recomputed_relation(
    sentence: str,
    arguments: Sequence[ClaimArgument],
    fields: Mapping[str, Mapping[str, str]],
) -> tuple[str, dict[str, Any]] | None:
    record_ids = tuple(dict.fromkeys(argument.record_id for argument in arguments))
    if "서로 다른" in sentence and len(record_ids) >= 2:
        return "set_distinct", {
            "matched": len(set(record_ids)) == len(record_ids),
            "record_ids": list(record_ids),
        }

    positioned = sorted(
        (
            sentence.casefold().find(value.casefold()),
            argument.record_id,
            argument.field_path,
            value,
        )
        for argument in arguments
        if (
            value := fields.get(argument.record_id, {}).get(argument.field_path, "")
        )
        and sentence.casefold().find(value.casefold()) >= 0
    )
    numeric_values = tuple(
        (record_id, field_path, value, _numeric_value(value))
        for _position, record_id, field_path, value in positioned
        if _numeric_value(value) is not None
    )
    if len(numeric_values) >= 2:
        left = numeric_values[0]
        right = numeric_values[1]
        direction = _numeric_direction(sentence)
        if (
            direction is not None
            and _numeric_dimension(left[1], left[2])
            == _numeric_dimension(right[1], right[2])
        ):
            matched = (
                left[3] > right[3] if direction == "gt" else left[3] < right[3]
            )
            if matched:
                return "numeric_comparison", {
                    "matched": True,
                    "direction": direction,
                    "left": {
                        "record_id": left[0],
                        "field_path": left[1],
                        "value": left[2],
                    },
                    "right": {
                        "record_id": right[0],
                        "field_path": right[1],
                        "value": right[2],
                    },
                }

    temporal_values = tuple(
        (record_id, field_path, value)
        for _position, record_id, field_path, value in positioned
        if _DATE_RE.fullmatch(value)
    )
    if len(temporal_values) >= 2:
        left = temporal_values[0]
        right = temporal_values[1]
        direction = _temporal_direction(sentence)
        if (
            direction is not None
            and _temporal_dimension(left[1]) == _temporal_dimension(right[1])
        ):
            matched = (
                left[2] < right[2] if direction == "before" else left[2] > right[2]
            )
            if matched:
                return "temporal_order", {
                    "matched": True,
                    "direction": direction,
                    "left": {
                        "record_id": left[0],
                        "field_path": left[1],
                        "value": left[2],
                    },
                    "right": {
                        "record_id": right[0],
                        "field_path": right[1],
                        "value": right[2],
                    },
                }
    return None


def _numeric_value(value: str) -> float | None:
    if _NUMBER_RE.fullmatch(value) is None:
        return None
    try:
        return float(value.rstrip("%").replace(",", ""))
    except ValueError:
        return None


def _numeric_dimension(field_path: str, value: str) -> str:
    normalized_path = field_path.casefold()
    leaf = re.sub(r"\[\d+\]", "", normalized_path.rsplit(".", 1)[-1])
    if value.endswith("%") or any(
        token in leaf for token in ("share", "rate", "ratio", "percent")
    ):
        return "percent"
    currency_codes = _CURRENCY_CODE_RE.findall(leaf)
    if any(
        token in leaf
        for token in (
            "sales",
            "revenue",
            "amount",
            "price",
            "cost",
            "krw",
            "usd",
            "jpy",
            "eur",
            "gbp",
            "cny",
        )
    ):
        if currency_codes:
            return f"currency:{currency_codes[-1].casefold()}"
        if "krw" in leaf or "억원" in value or "원" in value:
            return "currency:krw"
        if "usd" in leaf or "$" in value:
            return "currency:usd"
        return "currency:unspecified"
    return leaf


def _temporal_dimension(field_path: str) -> str:
    normalized_path = field_path.casefold()
    return re.sub(r"\[\d+\]", "", normalized_path.rsplit(".", 1)[-1])


def _numeric_direction(sentence: str) -> Literal["gt", "lt"] | None:
    lowered = sentence.casefold()
    if re.search(r"(?:높|크|많|증가|상회|greater|higher|larger)", lowered):
        return "gt"
    if re.search(r"(?:낮|작|적|감소|하회|less|lower|smaller)", lowered):
        return "lt"
    return None


def _temporal_direction(sentence: str) -> Literal["before", "after"] | None:
    lowered = sentence.casefold()
    if re.search(r"(?:먼저|앞서|이전|before|earlier)", lowered):
        return "before"
    if re.search(r"(?:나중|뒤|이후|after|later)", lowered):
        return "after"
    return None


def _stable_id(prefix: str, values: Sequence[str]) -> str:
    raw = json.dumps(tuple(values), ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}-{sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _density_metrics(
    answer: str,
    claims: Sequence[ClaimIR],
    records: Sequence[EvidenceRecord],
    fields: Mapping[str, Mapping[str, str]],
    supported_record_ids: set[str],
) -> dict[str, Any]:
    counts = defaultdict(int)
    for claim in claims:
        counts[claim.claim_type] += 1
    validated = counts["T1"] + counts["T2"]
    characters = max(len(answer), 1)
    field_coverage: dict[str, dict[str, float | int]] = {}
    for source in dict.fromkeys(record.source for record in records):
        source_records = [record for record in records if record.source == source]
        total_fields = 0
        populated = 0
        for record in source_records:
            record_total, record_populated = _field_population(record.payload)
            total_fields += record_total
            populated += record_populated
        field_coverage[source] = {
            "records": len(source_records),
            "populated_fields": populated,
            "target_fields": total_fields,
            "populated_field_rate": round(
                populated / total_fields if total_fields else 1.0,
                6,
            ),
        }
    return {
        "narrative_record_coverage": round(
            len(supported_record_ids) / len(records) if records else 1.0,
            6,
        ),
        "validated_claims_per_1k_chars": round(validated * 1000 / characters, 6),
        "claim_type_counts": {key: counts[key] for key in ("T1", "T2", "T3")},
        "claim_type_ratio": {
            key: round(counts[key] / len(claims), 6) if claims else 0.0
            for key in ("T1", "T2", "T3")
        },
        "field_coverage_by_source": field_coverage,
    }


def _field_population(value: Any) -> tuple[int, int]:
    if isinstance(value, Mapping):
        totals = [
            _field_population(item)
            for key, item in value.items()
            if not is_request_metadata_key(str(key))
        ]
        return sum(item[0] for item in totals), sum(item[1] for item in totals)
    if isinstance(value, (list, tuple)):
        if not value:
            return 1, 0
        totals = [_field_population(item) for item in value]
        return sum(item[0] for item in totals), sum(item[1] for item in totals)
    return 1, int(value not in (None, "", "원천 미제공"))
