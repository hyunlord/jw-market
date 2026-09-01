from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
import json
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.claim_ir import ClaimArgument, ClaimIR
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.narrative_compaction import (
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
    narrative_field_value,
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
    narrated_record_ids: tuple[str, ...] = ()
    unnarrated_records: tuple[dict[str, str], ...] = ()
    table_reference_record_ids: tuple[str, ...] = ()
    record_field_usage: tuple[dict[str, Any], ...] = ()
    average_narrated_field_count: float = 0.0
    loaded_field_narrative_use_rate: float = 0.0
    identifier_only_sentence_count: int = 0


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
    table_ids = frozenset(rendered_ids if table_record_ids is None else table_record_ids)
    plan = build_compaction_plan(records, table_ids)
    micro_node, t1_claims, unnarrated_records, field_usage = _micro_narratives(records)
    narrated_record_ids = micro_node.record_ids if micro_node is not None else ()
    available_field_count = sum(item["available_field_count"] for item in field_usage)
    used_field_count = sum(item["used_field_count"] for item in field_usage)
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
        )
        if node
    )
    return NarrativeRealization(
        nodes=nodes,
        claims=(*t1_claims, *t2_claims),
        recomputations=tuple(item.recomputation for item in t2_claims),
        truncated_t2_count=0,
        unnarrated_record_count=len(unnarrated_records),
        narrated_record_ids=narrated_record_ids,
        unnarrated_records=unnarrated_records,
        table_reference_record_ids=(),
        record_field_usage=field_usage,
        average_narrated_field_count=round(
            used_field_count / len(narrated_record_ids), 6
        ) if narrated_record_ids else 0.0,
        loaded_field_narrative_use_rate=round(
            used_field_count / available_field_count, 6
        ) if available_field_count else 0.0,
        identifier_only_sentence_count=sum(
            item["used_field_count"] == 0 and item["reason_code"] is None
            for item in field_usage
        ),
    )


def measure_final_narrative_surface(
    answer: str,
    evidence_sets: Sequence[EvidenceSet],
    record_field_usage: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Measure record narration on the final user-visible prose surface."""

    records_by_id = {
        record.evidence_id: record
        for evidence_set in evidence_sets
        for record in evidence_set.records
    }
    prose_blocks = _final_surface_prose_blocks(answer)
    narrated_ids: list[str] = []
    unnarrated: list[dict[str, str]] = []
    surfaced_used_fields = 0
    available_fields = 0
    identifier_only = 0
    per_record: list[dict[str, Any]] = []
    for index, usage in enumerate(record_field_usage, start=1):
        record_id = str(usage.get("record_id") or "")
        available_count = int(usage.get("available_field_count") or 0)
        available_fields += available_count
        record = records_by_id.get(record_id)
        identity = record_identity(record, index) if record is not None else None
        matching_blocks = tuple(
            block for block in prose_blocks if identity and identity in block
        )
        used_fields = tuple(str(field) for field in usage.get("used_fields") or ())
        matched_fields: tuple[str, ...] = ()
        if record is not None and matching_blocks:
            matched_fields = max(
                (
                    tuple(
                        field
                        for field in used_fields
                        if (
                            value := narrative_field_value(record, field)
                        ) is not None
                        and _normalized_surface_text(value)
                        in _normalized_surface_text(block)
                    )
                    for block in matching_blocks
                ),
                key=len,
                default=(),
            )
        matched_count = len(matched_fields)
        if matching_blocks:
            narrated_ids.append(record_id)
            surfaced_used_fields += matched_count
            identifier_only += int(matched_count == 0)
            reason_code = None
        else:
            reason_code = "public_identifier_missing_from_final_prose"
            unnarrated.append({"record_id": record_id, "reason_code": reason_code})
        per_record.append(
            {
                **usage,
                "public_identifier": identity,
                "final_surface_used_field_count": matched_count,
                "final_surface_used_fields": matched_fields,
                "final_surface_reason_code": reason_code,
            }
        )
    rendered_count = len(record_field_usage)
    return {
        "narrated_record_count": len(narrated_ids),
        "narrated_record_ids": narrated_ids,
        "unnarrated_record_count": len(unnarrated),
        "unnarrated_records": unnarrated,
        "narrative_identifier_parity": len(narrated_ids) == rendered_count,
        "narrative_record_accounting_complete": (
            len(narrated_ids) + len(unnarrated) == rendered_count
        ),
        "record_field_usage": per_record,
        "average_narrated_field_count": round(
            surfaced_used_fields / len(narrated_ids), 6
        ) if narrated_ids else 0.0,
        "loaded_field_narrative_use_rate": round(
            surfaced_used_fields / available_fields, 6
        ) if available_fields else 0.0,
        "identifier_only_sentence_count": identifier_only,
    }


def _final_surface_prose_blocks(answer: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    in_sources = False
    in_table = False
    for raw_line in answer.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if current:
                blocks.append(" ".join(current))
                current = []
            in_sources = line == "## 출처"
            in_table = False
            continue
        if in_sources:
            continue
        if line.startswith("|"):
            if current:
                blocks.append(" ".join(current))
                current = []
            in_table = True
            continue
        if in_table and line:
            in_table = False
        if not line or line.startswith(("#", "```")):
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if line.startswith("- ") and current:
            blocks.append(" ".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    return tuple(blocks)


def _normalized_surface_text(value: str) -> str:
    return " ".join(value.split())


def _micro_narratives(
    records: Sequence[EvidenceRecord],
) -> tuple[
    RenderNode | None,
    tuple[RealizedClaim, ...],
    tuple[dict[str, str], ...],
    tuple[dict[str, Any], ...],
]:
    claims: list[RealizedClaim] = []
    lines: list[str] = []
    surface_fields: list[str] = []
    narrated_record_ids: list[str] = []
    unnarrated_records: list[dict[str, str]] = []
    field_usage: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        fields = tuple(
            field
            for field in NARRATIVE_FIELDS
            if narrative_field_value(record, field) is not None
            and not (
                field == "official_title"
                and narrative_field_value(record, "brief_title")
                == narrative_field_value(record, "official_title")
            )
        )
        identity = record_identity(record, index)
        if identity is None:
            unnarrated_records.append(
                {
                    "record_id": record.evidence_id,
                    "reason_code": "public_identifier_missing",
                }
            )
            field_usage.append(
                {
                    "record_id": record.evidence_id,
                    "source": record.source,
                    "available_field_count": len(fields),
                    "used_field_count": 0,
                    "used_fields": (),
                    "reason_code": "public_identifier_missing",
                }
            )
            continue
        if not fields:
            unnarrated_records.append(
                {
                    "record_id": record.evidence_id,
                    "reason_code": "public_narrative_fields_missing",
                }
            )
            field_usage.append(
                {
                    "record_id": record.evidence_id,
                    "source": record.source,
                    "available_field_count": 0,
                    "used_field_count": 0,
                    "used_fields": (),
                    "reason_code": "public_narrative_fields_missing",
                }
            )
            continue
        values = tuple(narrative_field_value(record, field) or "" for field in fields)
        details = ", ".join(
            f"{FIELD_LABELS.get(field, field)} {value}"
            for field, value in zip(fields, values, strict=True)
        )
        citation = _inline_citation(record)
        sentence = (
            f"{public_source_label(record.source)}의 {identity}은(는) "
            f"{details}로 확인됩니다. {citation}"
        )
        lines.append(f"- {sentence}")
        narrated_record_ids.append(record.evidence_id)
        surface_fields.extend(fields)
        claims.append(_field_claim(record, fields, values, sentence))
        field_usage.append(
            {
                "record_id": record.evidence_id,
                "source": record.source,
                "available_field_count": len(fields),
                "used_field_count": len(fields),
                "used_fields": fields,
                "reason_code": None,
            }
        )
    if not claims:
        return None, (), tuple(unnarrated_records), tuple(field_usage)
    node = (
        RenderNode(
            block_id="narrative:field-restatement",
            record_ids=tuple(narrated_record_ids),
            surface_fields=tuple(dict.fromkeys(surface_fields)),
            text="\n".join(lines),
        )
        if lines
        else None
    )
    return node, tuple(claims), tuple(unnarrated_records), tuple(field_usage)


def _inline_citation(record: EvidenceRecord) -> str:
    if record.source == "web":
        publisher = field_value(record, "publisher")
        published_at = field_value(record, "published_at")
        title = field_value(record, "title")
        if publisher and published_at and title:
            return f"[출처: {publisher} · {published_at} · 「{title}」]"
    return f"[출처: {public_source_label(record.source)}]"


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
    "measure_final_narrative_surface",
    "verify_recomputation",
]
