from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.evidence_payload import is_request_metadata_key
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

_RENDER_AXIS_TOKENS = frozenset(
    {
        "월별",
        "분기별",
        "연도별",
        "반기",
        "주간",
        "입원/외래",
        "성별",
        "연령",
        "채널",
        "지역",
    }
)
_RECENT_MONTH_AXIS_RE = re.compile(r"최근\s*\d{1,3}\s*개월")


def render_axis_tokens(entities: Sequence[str]) -> tuple[str, ...]:
    return tuple(entity for entity in entities if _is_render_axis_token(entity))


def _completion_entities(plan: PlannerOutput) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            entity.strip()
            for entity in plan.requested_answer_shape.entities
            if entity.strip() and ":" not in entity and not _is_render_axis_token(entity)
        )
    )


def _is_render_axis_token(value: str) -> bool:
    normalized = " ".join(value.split())
    return (
        normalized in _RENDER_AXIS_TOKENS
        or _RECENT_MONTH_AXIS_RE.fullmatch(normalized) is not None
    )


def source_tier(plan: PlannerOutput, source: str) -> int:
    if source in plan.answer_sources:
        return 0
    if source in {"web"}:
        return 1
    return 1


def fan_out_tier_zero_queries(plan: PlannerOutput) -> PlannerOutput:
    entities = _completion_entities(plan)
    if len(entities) < 2:
        return plan

    updates: dict[str, tuple[str, ...]] = {}
    for source in plan.answer_sources:
        queries = getattr(plan.tool_queries, source)
        if source == "clinicaltrials":
            continue
        fallback_tail = " ".join(
            _ATTRIBUTE_QUERY_LABELS.get(attribute, attribute)
            for attribute in plan.requested_answer_shape.measure_or_attribute
        ).strip()
        intent_tails: list[str] = []
        passthrough: list[str] = []
        for query in queries:
            tail = _entity_query_tail(query, entities)
            if not tail:
                tail = fallback_tail
            if not tail:
                passthrough.append(query)
                continue
            if tail not in intent_tails:
                intent_tails.append(tail)
        expanded = [
            f"{entity} {tail}"
            for tail in intent_tails
            for entity in entities
        ]
        expanded.extend(passthrough)
        normalized = tuple(dict.fromkeys(expanded))
        if normalized != tuple(queries):
            updates[source] = normalized

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
    entities = _completion_entities(plan)
    assignments = _assign_results(entities, results)
    rows: list[dict[str, str]] = []
    confirmed: list[str] = []
    missing: list[str] = []
    required_sources = set(plan.answer_sources)
    for entity in entities:
        matched = assignments.get(entity, ())
        primary_by_source = {
            source: tuple(result for result in matched if result.source == source)
            for source in plan.answer_sources
        }
        completed_sources = {
            source
            for source, source_results in primary_by_source.items()
            if any(result.status == "ok" for result in source_results)
        }
        if required_sources and completed_sources == required_sources:
            status = "COMPLETE"
            confirmed.append(entity)
        elif completed_sources:
            status = "PARTIAL"
            missing.append(entity)
        else:
            status = "FAILED"
            missing.append(entity)
        rows.append({"entity": entity, "status": status})
    scope_notice = ""
    if confirmed and missing:
        entity_label = _entity_scope_label(plan, entities)
        missing_text = "·".join(missing)
        scope_notice = (
            f"확인된 {len(confirmed)}개 {entity_label}({'·'.join(confirmed)}) 기준으로 비교했으며, "
            f"{missing_text}{_topic_particle(missing_text)} 조회 결과와 연결하지 못했습니다."
        )
    missing_rows = "\n".join(
        f"| {row['entity']} | {row['status']} |"
        for row in rows
        if row["status"] != "COMPLETE"
    )
    return EntityCompletion(tuple(rows), scope_notice, missing_rows)


def _entity_scope_label(plan: PlannerOutput, entities: Sequence[str]) -> str:
    if "hira" in plan.answer_sources:
        return "상병코드·질환 항목"
    normalized = plan.resolved_question.casefold()
    kcd_like = any(re.fullmatch(r"[A-Za-z]\d{2,3}", entity.strip()) for entity in entities)
    if kcd_like or any(token in normalized for token in ("상병", "환자수", "질환")):
        return "상병코드·질환 항목"
    if "임상" in normalized:
        return "임상 대상"
    if "특허" in normalized:
        return "특허 대상"
    return "브랜드"


def _topic_particle(value: str) -> str:
    if not value:
        return "는"
    last = value[-1]
    if last.isdigit():
        return "은"
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def _assign_results(
    entities: Sequence[str],
    results: Sequence[SourceResult],
) -> dict[str, tuple[SourceResult, ...]]:
    assigned: dict[str, list[SourceResult]] = {entity: [] for entity in entities}
    ordered = sorted(entities, key=len, reverse=True)
    for result in results:
        payload_text = _payload_text(result.payload).strip().casefold()
        payload_matches = _entity_mentions(payload_text, ordered)
        if payload_matches:
            for entity in payload_matches:
                assigned[entity].append(result)
            continue
        if payload_text:
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
    tail = re.sub(r"(?:^|\s)(?:및|와|과)(?=\s|$)", " ", tail)
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
        return " ".join(
            f"{key} {_payload_text(value)}"
            for key, value in payload.items()
            if not is_request_metadata_key(str(key))
        )
    if isinstance(payload, (list, tuple)):
        return " ".join(_payload_text(value) for value in payload)
    return str(payload or "")
