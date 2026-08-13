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
from jw_chat_agent_poc.service.v4.narrative_compaction import (
    CompactionPlan,
    build_compaction_plan,
    relation_node,
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
    display_field_value,
    field_value,
    record_identity,
)
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


MAX_NARRATED_RECORDS: Final = 2_147_483_647  # Compatibility export; narration is uncapped.
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
            "CER",
            "GROWTH_DECOMP",
            "PRICE_MIX_INDEX",
            "PEER_ZSCORE",
            "CONCENTRATION_CR5",
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
    table_reference_record_ids: tuple[str, ...] = ()


def build_narrative_realization(
    evidence_sets: Sequence[EvidenceSet],
    rendered_ids: Sequence[str],
    *,
    table_record_ids: Sequence[str] | None = None,
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
    narrated = records
    unnarrated_count = 0
    table_ids = frozenset(rendered_ids if table_record_ids is None else table_record_ids)
    plan = build_compaction_plan(records, table_ids)
    micro_node, t1_claims = _micro_narratives(narrated, plan)
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
    t2_claims = t2_candidates
    nodes = tuple(
        node
        for node in (
            micro_node,
            relation_node(t2_claims, records_by_id, plan),
            _cross_source_fusion_node(
                evidence_sets,
                frozenset(record.evidence_id for record in records),
            ),
        )
        if node
    )
    return NarrativeRealization(
        nodes=nodes,
        claims=(*t1_claims, *t2_claims),
        recomputations=tuple(item.recomputation for item in t2_claims),
        truncated_t2_count=0,
        unnarrated_record_count=unnarrated_count,
        table_reference_record_ids=(),
    )


def _micro_narratives(
    records: Sequence[EvidenceRecord],
    plan: CompactionPlan,
) -> tuple[RenderNode | None, tuple[RealizedClaim, ...]]:
    claims: list[RealizedClaim] = []
    lines: list[str] = []
    surface_fields: list[str] = []
    for index, record in enumerate(records, start=1):
        fields = tuple(
            field for field in NARRATIVE_FIELDS if field_value(record, field) is not None
        )[:3]
        identity = record_identity(record, index)
        if len(fields) < 3 or identity is None:
            continue
        values = tuple(field_value(record, field) or "" for field in fields)
        display_values = tuple(
            display_field_value(record, field) or "" for field in fields
        )
        details = ", ".join(
            f"{FIELD_LABELS.get(field, field)} {value}"
            for field, value in zip(fields, display_values, strict=True)
        )
        citation = _inline_citation(record)
        sentence = (
            f"{public_source_label(record.source)}의 {identity}은(는) "
            f"{details}로 확인됩니다. {citation}"
        )
        if record.evidence_id not in plan.record_ids:
            lines.append(f"- {sentence}")
            surface_fields.extend(fields)
        claims.append(_field_claim(record, fields, values, sentence))
    if not claims:
        return None, ()
    node = (
        RenderNode(
            block_id="narrative:field-restatement",
            record_ids=tuple(
                record.evidence_id
                for record in records
                if record.evidence_id not in plan.record_ids
            ),
            surface_fields=tuple(dict.fromkeys(surface_fields)),
            text="\n".join(lines),
        )
        if lines
        else None
    )
    return node, tuple(claims)


def _inline_citation(record: EvidenceRecord) -> str:
    if record.source == "web":
        publisher = field_value(record, "publisher")
        published_at = field_value(record, "published_at")
        title = field_value(record, "title")
        if publisher and published_at and title:
            return f"[출처: {publisher} · {published_at} · 「{title}」]"
    return f"[출처: {public_source_label(record.source)}]"


def _cross_source_fusion_node(
    evidence_sets: Sequence[EvidenceSet],
    rendered_ids: frozenset[str],
) -> RenderNode | None:
    available = tuple(
        (
            evidence_set,
            tuple(
                record
                for record in evidence_set.records
                if record.evidence_id in rendered_ids
            ),
        )
        for evidence_set in evidence_sets
        if any(record.evidence_id in rendered_ids for record in evidence_set.records)
    )
    if len(available) < 2:
        return None
    anchor_set, anchor_records = available[0]
    anchor_fact = _source_fact_fragment(anchor_set, anchor_records)
    if anchor_fact is None:
        return None
    anchor_record, anchor_text = anchor_fact
    lines: list[str] = []
    bound_ids: list[str] = []
    for other_set, other_records in available[1:4]:
        other_fact = _source_fact_fragment(other_set, other_records)
        if other_fact is None:
            continue
        other_record, other_text = other_fact
        citations = "; ".join(
            (
                _citation_label(anchor_set, anchor_record),
                _citation_label(other_set, other_record),
            )
        )
        lines.append(
            f"{anchor_text} {other_text} 각각 확인했습니다. [출처: {citations}]"
        )
        bound_ids.extend(record.evidence_id for record in anchor_records)
        bound_ids.extend(record.evidence_id for record in other_records)
    if not lines:
        return None
    return RenderNode(
        block_id="narrative:cross-source-fusion",
        record_ids=tuple(dict.fromkeys(bound_ids)),
        text="\n".join(lines),
    )


def _source_fact_fragment(
    evidence_set: EvidenceSet,
    records: Sequence[EvidenceRecord],
) -> tuple[EvidenceRecord, str] | None:
    ranked = sorted(
        records,
        key=lambda record: (
            -sum(field_value(record, field) is not None for field in NARRATIVE_FIELDS),
            record.evidence_id,
        ),
    )
    for index, record in enumerate(ranked, start=1):
        identity = record_identity(record, index)
        fields = tuple(
            field
            for field in NARRATIVE_FIELDS
            if field not in {"sales_krw", "market_share"}
            and field_value(record, field) is not None
        )[:3]
        if identity is None or not fields:
            continue
        details = ", ".join(
            f"{FIELD_LABELS.get(field, field)} {display_field_value(record, field) or ''}"
            for field in fields
        )
        return (
            record,
            f"{public_source_label(evidence_set.source)}에서 {len(records)}건을 확인했고 "
            f"대표 항목 {identity}은(는) {details}로 나타났으며,",
        )
    return None


def _citation_label(evidence_set: EvidenceSet, record: EvidenceRecord) -> str:
    if evidence_set.source == "web":
        publisher = field_value(record, "publisher")
        published_at = field_value(record, "published_at")
        title = field_value(record, "title")
        if publisher and published_at and title:
            return f"{publisher} · {published_at} · 「{title}」"
    return public_source_label(evidence_set.source)


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


__all__ = [
    "ALLOWED_T2_OPERATORS",
    "NarrativeRealization",
    "build_narrative_realization",
    "verify_recomputation",
]
