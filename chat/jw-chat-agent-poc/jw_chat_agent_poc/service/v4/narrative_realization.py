from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
import json
from typing import Final

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.claim_ir import ClaimArgument, ClaimIR
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.narrative_relations import (
    ALLOWED_T2_OPERATORS,
    MAX_T2_CLAIMS,
    RealizedClaim,
    build_relation_claims,
)
from jw_chat_agent_poc.service.v4.narrative_recomputation import (
    RecomputationEvidence,
    verify_recomputation,
)
from jw_chat_agent_poc.service.v4.narrative_values import (
    FIELD_LABELS,
    NARRATIVE_FIELDS,
    field_value,
    record_identity,
)


MAX_NARRATED_RECORDS: Final = 8
_T2_OPERATOR_PRIORITY: Final = {
    operator: index
    for index, operator in enumerate(
        (
            "COUNT",
            "GROUP_COUNT",
            "COMMON_VALUE",
            "ORDER_BY_TIME",
            "SIMULTANEITY",
            "COMPARE_NUMERIC",
            "RANGE",
        )
    )
}


class NarrativeRealization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[RenderNode, ...]
    claims: tuple[RealizedClaim, ...]
    recomputations: tuple[RecomputationEvidence, ...]
    truncated_t2_count: int = 0
    unnarrated_record_count: int = 0


def build_narrative_realization(
    evidence_sets: Sequence[EvidenceSet],
    rendered_ids: Sequence[str],
) -> NarrativeRealization:
    records_by_id = {
        record.evidence_id: record
        for evidence_set in evidence_sets
        for record in evidence_set.records
    }
    records = tuple(
        records_by_id[record_id]
        for record_id in dict.fromkeys(rendered_ids)
        if record_id in records_by_id
    )
    narrated = records[:MAX_NARRATED_RECORDS]
    unnarrated_count = max(0, len(records) - len(narrated))
    micro_node, t1_claims = _micro_narratives(narrated, unnarrated_count)
    t2_candidates = tuple(
        sorted(
            build_relation_claims(
                evidence_sets,
                frozenset(record.evidence_id for record in records),
            ),
            key=lambda item: (
                -len(item.recomputation.record_ids),
                _T2_OPERATOR_PRIORITY[item.claim.operator_id],
                item.recomputation.record_ids,
                item.claim.claim_id,
            ),
        )
    )
    t2_claims = t2_candidates[:MAX_T2_CLAIMS]
    nodes = tuple(node for node in (micro_node, _relation_node(t2_claims)) if node)
    return NarrativeRealization(
        nodes=nodes,
        claims=(*t1_claims, *t2_claims),
        recomputations=tuple(item.recomputation for item in t2_claims),
        truncated_t2_count=max(0, len(t2_candidates) - len(t2_claims)),
        unnarrated_record_count=unnarrated_count,
    )


def _micro_narratives(
    records: Sequence[EvidenceRecord],
    unnarrated_count: int,
) -> tuple[RenderNode | None, tuple[RealizedClaim, ...]]:
    claims: list[RealizedClaim] = []
    lines: list[str] = []
    surface_fields: list[str] = []
    for index, record in enumerate(records, start=1):
        fields = tuple(
            field for field in NARRATIVE_FIELDS if field_value(record, field) is not None
        )[:3]
        if not fields:
            continue
        values = tuple(field_value(record, field) or "" for field in fields)
        details = ", ".join(
            f"{FIELD_LABELS.get(field, field)} {value}"
            for field, value in zip(fields, values, strict=True)
        )
        sentence = f"{record_identity(record, index)}은(는) {details}로 확인됩니다."
        lines.append(f"- {sentence}")
        surface_fields.extend(fields)
        claims.append(_field_claim(record, fields, values, sentence))
    if not lines:
        return None, ()
    if unnarrated_count:
        lines.append(
            f"- 나머지 {unnarrated_count}건은 아래 정본 표에서 확인할 수 있습니다."
        )
    return (
        RenderNode(
            block_id="narrative:field-restatement",
            record_ids=tuple(record.evidence_id for record in records),
            surface_fields=tuple(dict.fromkeys(surface_fields)),
            text="## [직접 확인] 레코드 설명\n" + "\n".join(lines),
        ),
        tuple(claims),
    )


def _field_claim(
    record: EvidenceRecord,
    fields: Sequence[str],
    values: Sequence[str],
    sentence: str,
) -> RealizedClaim:
    arguments = tuple(
        ClaimArgument(
            record_id=record.evidence_id,
            field_path=f"payload.{field}",
            value_hash=sha256(value.encode("utf-8")).hexdigest(),
        )
        for field, value in zip(fields, values, strict=True)
    )
    claim_digest = sha256(sentence.encode("utf-8")).hexdigest()
    support_digest = sha256(
        json.dumps((record.evidence_id, *fields)).encode("utf-8")
    ).hexdigest()
    proof = RecomputationEvidence(
        operator_id="COUNT",
        record_ids=(record.evidence_id,),
        expected=1,
    )
    return RealizedClaim(
        claim=ClaimIR(
            claim_id=f"CLAIM-REALIZED-{claim_digest[:16]}",
            claim_type="T1",
            predicate_id="field_restatement",
            arguments=arguments,
            support_set_id=f"SUPPORT-{support_digest[:16]}",
            operator_id="field_restatement",
            entity_scope=(record.evidence_id,),
            causal_level="NONE",
            modality="OBSERVED",
        ),
        text=sentence,
        recomputation=proof,
    )


def _relation_node(claims: Sequence[RealizedClaim]) -> RenderNode | None:
    if not claims:
        return None
    record_ids = tuple(
        dict.fromkeys(
            record_id
            for item in claims
            for record_id in item.recomputation.record_ids
        )
    )
    return RenderNode(
        block_id="narrative:cross-record-relations",
        record_ids=record_ids,
        text="## [직접 확인] 레코드 관계\n"
        + "\n".join(f"- {item.text}" for item in claims),
    )


__all__ = [
    "ALLOWED_T2_OPERATORS",
    "NarrativeRealization",
    "build_narrative_realization",
    "verify_recomputation",
]
