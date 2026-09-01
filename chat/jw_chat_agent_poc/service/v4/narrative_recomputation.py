from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
import math
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
    "CER",
    "GROWTH_DECOMP",
    "PRICE_MIX_INDEX",
    "PEER_ZSCORE",
    "CONCENTRATION_CR5",
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
        "CER",
        "GROWTH_DECOMP",
        "PRICE_MIX_INDEX",
        "PEER_ZSCORE",
        "CONCENTRATION_CR5",
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
    if operator_id in {"CER", "GROWTH_DECOMP", "PRICE_MIX_INDEX"}:
        fields = tuple(
            part.removeprefix("payload.")
            for part in (field_path or "").split("|")
            if part
        )
        if len(records) != 1 or len(fields) < 2:
            return None
        numbers = tuple(
            numeric_value(field_value(records[0], field)) for field in fields
        )
        if any(value is None for value in numbers):
            return None
        resolved = tuple(float(value) for value in numbers if value is not None)
        if operator_id in {"CER", "PRICE_MIX_INDEX"}:
            return None if resolved[1] == 0 else round(resolved[0] / resolved[1], 8)
        market_contribution = resolved[1]
        share_contribution = resolved[2] if len(resolved) > 2 else resolved[0] - resolved[1]
        return {
            "brand_growth": resolved[0],
            "market_contribution": market_contribution,
            "share_contribution": share_contribution,
            "recomputed_growth": market_contribution + share_contribution,
        }
    if operator_id in {"PEER_ZSCORE", "CONCENTRATION_CR5"}:
        field = (field_path or "").removeprefix("payload.")
        pairs = tuple(
            (record.evidence_id, numeric_value(field_value(record, field)))
            for record in records
        )
        values = tuple(float(value) for _record_id, value in pairs if value is not None)
        if operator_id == "CONCENTRATION_CR5":
            return round(sum(sorted(values, reverse=True)[:5]), 8) if values else None
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        deviation = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
        return {
            record_id: (0.0 if deviation == 0 else round((float(value) - mean) / deviation, 8))
            for record_id, value in pairs
            if value is not None
        }
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
