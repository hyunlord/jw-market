from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from hashlib import sha256
import json
from typing import Final

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.claim_ir import ClaimArgument, ClaimIR
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.narrative_recomputation import (
    ALLOWED_T2_OPERATORS,
    T2Operator,
    RecomputationEvidence,
    compute_value,
)
from jw_chat_agent_poc.service.v4.narrative_values import (
    DATE_FIELDS,
    FIELD_LABELS,
    GROUP_FIELDS,
    NUMERIC_FIELDS,
    display_number,
    field_value,
    numeric_value,
)
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


MAX_T2_CLAIMS: Final = 20


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RealizedClaim(_FrozenModel):
    claim: ClaimIR
    text: str
    recomputation: RecomputationEvidence


def build_relation_claims(
    evidence_sets: Sequence[EvidenceSet],
    rendered_ids: frozenset[str],
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for evidence_set in evidence_sets:
        records = tuple(
            record for record in evidence_set.records if record.evidence_id in rendered_ids
        )
        if len(records) < 2:
            continue
        label = public_source_label(evidence_set.source)
        output.append(
            _relation(
                "COUNT",
                records,
                None,
                f"{label}에서 확인된 레코드는 {len(records)}건입니다.",
            )
        )
        output.extend(_field_relations(records, label))
    return tuple(output)


def _field_relations(
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for field in GROUP_FIELDS:
        field_records = tuple(
            record for record in records if field_value(record, field) is not None
        )
        if len(field_records) < 2:
            continue
        values = tuple(field_value(record, field) for record in field_records)
        counts = Counter(value for value in values if value is not None)
        label = FIELD_LABELS.get(field, field)
        partial = len(field_records) < len(records)
        if len(counts) > 1:
            groups = ", ".join(
                f"{value} {count}건" for value, count in sorted(counts.items())
            )
            prefix = (
                f"{source_label}에서 {label}가 제공된 레코드 기준으로"
                if partial
                else f"{source_label} 레코드는 {label}별로"
            )
            output.append(
                _relation(
                    "GROUP_COUNT",
                    field_records,
                    field,
                    f"{prefix} {groups}입니다.",
                )
            )
        elif counts:
            common = next(iter(counts))
            subject = (
                f"{source_label}에서 {label}가 제공된 레코드는"
                if partial
                else f"{source_label}에서 확인된 레코드는"
            )
            output.append(
                _relation(
                    "COMMON_VALUE",
                    field_records,
                    field,
                    f"{subject} 모두 {label} {common}입니다.",
                )
            )
    output.extend(_date_relations(records, source_label))
    for field in NUMERIC_FIELDS:
        field_records = tuple(
            record
            for record in records
            if numeric_value(field_value(record, field)) is not None
        )
        values = tuple(numeric_value(field_value(record, field)) for record in field_records)
        present = tuple(value for value in values if value is not None)
        if len(present) >= 2 and len(set(present)) > 1:
            output.append(
                _relation(
                    "COMPARE_NUMERIC",
                    field_records,
                    field,
                    f"{source_label} 레코드의 {FIELD_LABELS.get(field, field)}은 최소 "
                    f"{display_number(min(present))}, 최대 {display_number(max(present))}입니다.",
                )
            )
    return tuple(output)


def _date_relations(
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for field in DATE_FIELDS:
        field_records = tuple(
            record for record in records if field_value(record, field) is not None
        )
        values = tuple(field_value(record, field) for record in field_records)
        present = tuple(value for value in values if value is not None)
        if len(present) < 2:
            continue
        output.append(
            _relation(
                "RANGE",
                field_records,
                field,
                f"{source_label} 레코드의 {FIELD_LABELS.get(field, field)} 범위는 "
                f"{min(present)}부터 "
                f"{max(present)}까지입니다.",
            )
        )
        if len(set(present)) > 1:
            output.append(
                _relation(
                    "ORDER_BY_TIME",
                    field_records,
                    field,
                    f"{source_label} 레코드의 {FIELD_LABELS.get(field, field)} 순으로 "
                    f"보면 {min(present)}이 "
                    f"가장 이르고 {max(present)}이 가장 늦습니다.",
                )
            )
        simultaneous = Counter(present).most_common(1)[0]
        if simultaneous[1] >= 2:
            output.append(
                _relation(
                    "SIMULTANEITY",
                    field_records,
                    field,
                    f"{source_label} 레코드는 {simultaneous[0]}에 "
                    f"{simultaneous[1]}건이 같은 시점으로 확인됩니다.",
                )
            )
    return tuple(output)


def _relation(
    operator_id: T2Operator,
    records: Sequence[EvidenceRecord],
    field: str | None,
    sentence: str,
) -> RealizedClaim:
    record_ids = tuple(record.evidence_id for record in records)
    field_path = f"payload.{field}" if field else None
    proof = RecomputationEvidence(
        operator_id=operator_id,
        record_ids=record_ids,
        field_path=field_path,
        expected=compute_value(operator_id, records, field_path),
    )
    arguments = tuple(
        ClaimArgument(
            record_id=record.evidence_id,
            field_path=field_path or "evidence_id",
            value_hash=sha256(
                (field_value(record, field) if field else record.evidence_id).encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        for record in records
    )
    digest = sha256(sentence.encode("utf-8")).hexdigest()
    support = sha256(json.dumps(record_ids).encode("utf-8")).hexdigest()
    return RealizedClaim(
        claim=ClaimIR(
            claim_id=f"CLAIM-REALIZED-{digest[:16]}",
            claim_type="T2",
            predicate_id=operator_id,
            arguments=arguments,
            support_set_id=f"SUPPORT-{support[:16]}",
            operator_id=operator_id,
            entity_scope=record_ids,
            causal_level="NONE",
            modality="OBSERVED",
        ),
        text=sentence,
        recomputation=proof,
    )
