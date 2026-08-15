from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, RenderNode
from jw_chat_agent_poc.service.v4.narrative_relations import RealizedClaim
from jw_chat_agent_poc.service.v4.narrative_values import NARRATIVE_FIELDS, field_value
_SUMMARY_FIELDS: Final = {
    "overall_status": 1,
    "status": 1,
    "phase": 2,
    "phases": 2,
    "sponsor": 3,
    "countries": 3,
}
_SUMMARY_OPERATORS: Final = frozenset({"COUNT", "GROUP_COUNT", "COMMON_VALUE"})


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    record_ids: frozenset[str]
    sources: frozenset[str]


def build_compaction_plan(
    records: Sequence[EvidenceRecord],
    table_record_ids: frozenset[str],
) -> CompactionPlan:
    groups: dict[tuple[str, tuple[str, ...]], list[EvidenceRecord]] = {}
    for record in records:
        predicate = tuple(
            field
            for field in NARRATIVE_FIELDS
            if field_value(record, field) is not None
        )[:3]
        if predicate:
            groups.setdefault((record.source, predicate), []).append(record)
    compacted_groups = tuple(
        ((source, predicate), tuple(group))
        for (source, predicate), group in groups.items()
        if len(group) >= 3
        and all(record.evidence_id in table_record_ids for record in group)
    )
    return CompactionPlan(
        record_ids=frozenset(
            record.evidence_id for _, group in compacted_groups for record in group
        ),
        sources=frozenset(source for (source, _), _ in compacted_groups),
    )


def source_heading(source: str) -> str:
    """Compatibility helper for callers outside narrative realization."""
    return source


def relation_node(
    claims: Sequence[RealizedClaim],
    records_by_id: Mapping[str, EvidenceRecord],
    plan: CompactionPlan,
) -> RenderNode | None:
    by_source: dict[str, list[RealizedClaim]] = {}
    for item in claims:
        source = records_by_id[item.recomputation.record_ids[0]].source
        by_source.setdefault(source, []).append(item)
    candidates = {
        source: tuple(
            sorted(
                (
                    item
                    for item in items
                    if item.claim.operator_id in _SUMMARY_OPERATORS
                ),
                key=_summary_priority,
            )[:3]
        )
        if source in plan.sources
        else tuple(items)
        for source, items in by_source.items()
    }
    visible = {source: items for source, items in candidates.items() if items}
    if not visible:
        return None
    return RenderNode(
        block_id="narrative:cross-record-relations",
        record_ids=tuple(
            dict.fromkeys(
                record_id
                for items in visible.values()
                for item in items
                for record_id in item.recomputation.record_ids
            )
        ),
        text=" ".join(
            item.text for items in visible.values() for item in items
        ),
    )


def _summary_priority(item: RealizedClaim) -> tuple[int, str, str]:
    operator = item.claim.operator_id
    field = (item.recomputation.field_path or "").removeprefix("payload.")
    priority = {
        "COUNT": 0,
        "GROUP_COUNT": _SUMMARY_FIELDS.get(field, 4),
        "COMMON_VALUE": _SUMMARY_FIELDS.get(field, 4),
    }.get(operator, 4)
    return priority, operator, item.claim.claim_id


__all__ = ["CompactionPlan", "build_compaction_plan", "relation_node", "source_heading"]
