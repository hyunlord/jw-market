from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.narrative_values import field_value, numeric_value


T2Operator = Literal[
    "COUNT",
    "GROUP_COUNT",
    "COMMON_VALUE",
    "ORDER_BY_TIME",
    "SIMULTANEITY",
    "COMPARE_NUMERIC",
    "RANGE",
]
RecomputedValue = int | float | str | dict[str, int | float | str] | None
ALLOWED_T2_OPERATORS: Final[frozenset[str]] = frozenset(
    {
        "COUNT",
        "GROUP_COUNT",
        "COMMON_VALUE",
        "ORDER_BY_TIME",
        "SIMULTANEITY",
        "COMPARE_NUMERIC",
        "RANGE",
    }
)


class RecomputationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_id: T2Operator
    record_ids: tuple[str, ...]
    field_path: str | None = None
    expected: RecomputedValue
    matched: bool = True
    reason_code: str = "matched"


def verify_recomputation(
    proof: RecomputationEvidence,
    evidence_sets: Sequence[EvidenceSet],
) -> RecomputationEvidence:
    records = {
        record.evidence_id: record
        for evidence_set in evidence_sets
        for record in evidence_set.records
    }
    if any(record_id not in records for record_id in proof.record_ids):
        return proof.model_copy(
            update={"matched": False, "reason_code": "input_records_changed"}
        )
    actual = compute_value(
        proof.operator_id,
        tuple(records[record_id] for record_id in proof.record_ids),
        proof.field_path,
    )
    return proof.model_copy(
        update={
            "matched": actual == proof.expected,
            "reason_code": "matched" if actual == proof.expected else "value_mismatch",
        }
    )


def compute_value(
    operator_id: T2Operator,
    records: Sequence[EvidenceRecord],
    field_path: str | None,
) -> RecomputedValue:
    if operator_id == "COUNT":
        return len(records)
    field = field_path.removeprefix("payload.") if field_path else ""
    values = tuple(
        value for record in records if (value := field_value(record, field)) is not None
    )
    if operator_id == "GROUP_COUNT":
        return dict(sorted(Counter(values).items()))
    if operator_id == "COMMON_VALUE":
        return values[0] if values and len(set(values)) == 1 else None
    if operator_id == "ORDER_BY_TIME":
        return {"first": min(values), "last": max(values)} if values else None
    if operator_id == "SIMULTANEITY":
        value, count = Counter(values).most_common(1)[0]
        return {"value": value, "count": count}
    if operator_id == "COMPARE_NUMERIC":
        numbers = tuple(
            value for item in values if (value := numeric_value(item)) is not None
        )
        return {"min": min(numbers), "max": max(numbers)} if numbers else None
    return {"min": min(values), "max": max(values)} if values else None
