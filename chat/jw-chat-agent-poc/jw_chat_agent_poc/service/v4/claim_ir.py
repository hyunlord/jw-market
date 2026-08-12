from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet


ClaimType = Literal["T1", "T2", "T3"]
CausalLevel = Literal["NONE", "TEMPORAL", "ASSOCIATION", "CAUSAL"]
Modality = Literal["ASSERTED", "OBSERVED", "NOT_ESTABLISHED"]
_SENTENCE_RE = re.compile(r"(?<=[.!?。]|[다요음됨임])\s+(?=[^\s])")
_HIGH_ENTROPY_RE = re.compile(
    r"(?:\bNCT\d{8}\b|\b[A-Z]{2,}[A-Z0-9]*-\d+[A-Za-z]?\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|(?<!\w)\d+(?:\.\d+)?%?)",
    re.IGNORECASE,
)
_CAUSAL_RE = re.compile(r"(?:때문|원인|야기|일으켰|영향을\s*줬|caus)", re.IGNORECASE)


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
            if not any(token.casefold() in value.casefold() for value in _all_field_values(fields))
        )
        relation = _recomputed_relation(sentence, arguments, fields)
        if len(record_ids) == 1 and arguments and not unsupported_high_entropy:
            claim_type: ClaimType = "T1"
            operator_id = "field_restatement"
            predicate_id = "field_restatement"
            modality: Modality = "OBSERVED"
        elif len(record_ids) >= 2 and relation is not None and not unsupported_high_entropy:
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
                dict.fromkeys(match.group(0) for match in re.finditer(r"\b\d{4}(?:-\d{2}-\d{2})?\b", sentence))
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
    prose = " ".join(
        line.strip(" -*")
        for line in answer.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "|", "```"))
    )
    return tuple(item.strip() for item in _SENTENCE_RE.split(prose) if item.strip())


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


def _recomputed_relation(
    sentence: str,
    arguments: Sequence[ClaimArgument],
    _fields: Mapping[str, Mapping[str, str]],
) -> tuple[str, bool] | None:
    record_ids = tuple(dict.fromkeys(argument.record_id for argument in arguments))
    if "서로 다른" in sentence and len(record_ids) >= 2:
        return "set_distinct", len(set(record_ids)) == len(record_ids)
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
        totals = [_field_population(item) for item in value.values()]
        return sum(item[0] for item in totals), sum(item[1] for item in totals)
    if isinstance(value, (list, tuple)):
        if not value:
            return 1, 0
        totals = [_field_population(item) for item in value]
        return sum(item[0] for item in totals), sum(item[1] for item in totals)
    return 1, int(value not in (None, "", "원천 미제공"))
