from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet


@dataclass(frozen=True)
class EntityCompletion:
    rows: tuple[dict[str, str], ...]
    scope_notice: str
    missing_rows_markdown: str


_ATTRIBUTE_QUERY_LABELS = {
    "api_unit_price": "API 단가",
    "sales": "매출 현황",
    "market_share": "점유율",
    "patient_count": "환자수",
    "patent": "특허현황",
    "reimbursement": "급여기준",
    "active_clinical_trials": "진행 중 임상",
    "clinical_trials": "임상현황",
}


def source_tier(plan: PlannerOutput, source: str) -> int:
    if source in plan.answer_sources:
        return 0
    if source in {"web"}:
        return 2
    return 1


def fan_out_tier_zero_queries(plan: PlannerOutput) -> PlannerOutput:
    entities = tuple(
        dict.fromkeys(
            entity.strip()
            for entity in plan.requested_answer_shape.entities
            if entity.strip() and ":" not in entity
        )
    )
    if len(entities) < 2:
        return plan

    updates: dict[str, tuple[str, ...]] = {}
    for source in plan.answer_sources:
        queries = getattr(plan.tool_queries, source)
        if source == "clinicaltrials" or _queries_cover_entities(queries, entities):
            continue
        tail = _entity_query_tail(queries[0], entities)
        if not tail:
            tail = " ".join(
                _ATTRIBUTE_QUERY_LABELS.get(attribute, attribute)
                for attribute in plan.requested_answer_shape.measure_or_attribute
            ).strip()
        if not tail:
            continue
        updates[source] = tuple(f"{entity} {tail}" for entity in entities)

    if not updates:
        return plan
    return plan.model_copy(
        update={
            "tool_queries": plan.tool_queries.model_copy(update=updates),
        }
    )


def tier_funnel(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    evidence_sets: Sequence[EvidenceSet],
    rendered_record_ids: Sequence[str],
    claim_record_ids: Sequence[str] = (),
) -> dict[str, dict[str, int]]:
    funnel = {
        f"tier_{tier}": {
            "S1_queries": 0,
            "S2_results": 0,
            "S3_records": 0,
            "S4_rendered": 0,
            "S5_claim_records": 0,
        }
        for tier in range(3)
    }
    for source, queries in plan.tool_queries.items():
        funnel[f"tier_{source_tier(plan, source)}"]["S1_queries"] += len(queries)
    for result in results:
        if result.status == "ok":
            funnel[f"tier_{source_tier(plan, result.source)}"]["S2_results"] += 1
    record_tiers: dict[str, int] = {}
    for evidence_set in evidence_sets:
        tier = source_tier(plan, evidence_set.source)
        funnel[f"tier_{tier}"]["S3_records"] += len(evidence_set.records)
        for record in evidence_set.records:
            record_tiers[record.evidence_id] = tier
    for record_id in set(rendered_record_ids):
        tier = record_tiers.get(record_id)
        if tier is not None:
            funnel[f"tier_{tier}"]["S4_rendered"] += 1
    for record_id in set(claim_record_ids):
        tier = record_tiers.get(record_id)
        if tier is not None:
            funnel[f"tier_{tier}"]["S5_claim_records"] += 1
    return funnel


def entity_completion_rows(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
) -> EntityCompletion:
    entities = tuple(
        dict.fromkeys(
            entity
            for entity in plan.requested_answer_shape.entities
            if ":" not in entity
        )
    )
    assignments = _assign_results(entities, results)
    rows: list[dict[str, str]] = []
    confirmed: list[str] = []
    missing: list[str] = []
    for entity in entities:
        matched = assignments.get(entity, ())
        statuses = {result.status for result in matched}
        if "ok" in statuses:
            status = "PARTIAL" if statuses - {"ok"} else "COMPLETE"
            confirmed.append(entity)
        elif matched:
            status = "FAILED"
            missing.append(entity)
        else:
            status = "FAILED"
            missing.append(entity)
        rows.append({"entity": entity, "status": status})
    scope_notice = ""
    if confirmed and missing:
        scope_notice = (
            f"확인된 {len(confirmed)}개 브랜드({'·'.join(confirmed)}) 기준으로 비교했으며, "
            f"{'·'.join(missing)}는 미도착으로 제외했습니다."
        )
    missing_rows = "\n".join(
        f"| {row['entity']} | {row['status']} |"
        for row in rows
        if row["status"] != "COMPLETE"
    )
    return EntityCompletion(tuple(rows), scope_notice, missing_rows)


def _assign_results(
    entities: Sequence[str],
    results: Sequence[SourceResult],
) -> dict[str, tuple[SourceResult, ...]]:
    assigned: dict[str, list[SourceResult]] = {entity: [] for entity in entities}
    ordered = sorted(entities, key=len, reverse=True)
    for result in results:
        payload_text = _payload_text(result.payload).casefold()
        payload_matches = _entity_mentions(payload_text, ordered)
        if payload_matches:
            for entity in payload_matches:
                assigned[entity].append(result)
            continue
        query_matches = _entity_mentions(result.query.casefold(), ordered)
        if len(query_matches) == 1:
            assigned[query_matches[0]].append(result)
    return {entity: tuple(values) for entity, values in assigned.items()}


def _queries_cover_entities(
    queries: Sequence[str],
    entities: Sequence[str],
) -> bool:
    covered = {
        matches[0]
        for query in queries
        if len(matches := _entity_mentions(query.casefold(), entities)) == 1
    }
    return covered == set(entities)


def _entity_query_tail(query: str, entities: Sequence[str]) -> str:
    pattern = "|".join(re.escape(entity) for entity in sorted(entities, key=len, reverse=True))
    tail = re.sub(pattern, " ", query, flags=re.IGNORECASE)
    tail = re.sub(r"[,，:·/]+", " ", tail)
    return " ".join(tail.split()).strip()


def _entity_mentions(text: str, ordered_entities: Sequence[str]) -> list[str]:
    matches: list[str] = []
    occupied: list[tuple[int, int]] = []
    for entity in ordered_entities:
        for match in re.finditer(re.escape(entity.casefold()), text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            occupied.append(span)
            matches.append(entity)
            break
    return matches


def _payload_text(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return " ".join(f"{key} {_payload_text(value)}" for key, value in payload.items())
    if isinstance(payload, (list, tuple)):
        return " ".join(_payload_text(value) for value in payload)
    return str(payload or "")
