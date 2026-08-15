from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import json
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    LosslessInvariantError,
    RenderNode,
    RenderProfile,
    SourceReference,
)
from jw_chat_agent_poc.service.v4.narrative_realization import (
    build_narrative_realization,
    verify_recomputation,
)
from jw_chat_agent_poc.service.v4.render_clinical import (
    ACTIVE_CLINICAL_STATUSES,
    render_clinical,
)
from jw_chat_agent_poc.service.v4.render_common import text
from jw_chat_agent_poc.service.v4.render_patent import render_patent
from jw_chat_agent_poc.service.v4.render_policy import render_policy
from jw_chat_agent_poc.service.v4.retrieval_events import (
    classify_failure_signals,
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.service.v4.source_labels import public_source_label
from jw_chat_agent_poc.service.v4.source_tiers import source_tier


def render_deterministic_facts(
    plan: PlannerOutput,
    evidence_sets: Sequence[EvidenceSet],
    *,
    observed_on: date,
) -> DeterministicRender:
    profile = select_render_profile(plan, evidence_sets)
    source_notices, source_notice_bindings = (
        _source_failure_notices(evidence_sets)
        if _source_notices_enabled(plan, profile, evidence_sets)
        else ((), ())
    )
    source_tiers = {
        evidence_set.source: source_tier(plan, evidence_set.source)
        for evidence_set in evidence_sets
    }
    if profile == "market_analysis":
        return DeterministicRender(
            profile=profile,
            request_notice=_request_satisfaction_notice(plan, evidence_sets),
            source_notices=source_notices,
            source_notice_bindings=source_notice_bindings,
            source_tiers=source_tiers,
        )

    selected = _selected_set(profile, evidence_sets)
    if selected is None:
        return DeterministicRender(
            profile="market_analysis",
            source_notices=source_notices,
            source_notice_bindings=source_notice_bindings,
            source_tiers=source_tiers,
        )
    if profile in {"clinical_portfolio", "single_record_detail"}:
        nodes, required = render_clinical(
            selected,
            single=profile == "single_record_detail",
        )
    elif profile == "patent_portfolio":
        nodes, required = render_patent(selected, observed_on)
    else:
        nodes, required = render_policy(selected)

    nodes = _inject_missing_field_node(nodes, selected, required)
    auxiliary_sets = tuple(
        evidence_set
        for evidence_set in evidence_sets
        if evidence_set is not selected and evidence_set.records
    )
    nodes = _insert_auxiliary_nodes(plan, nodes, auxiliary_sets)
    rendered_sets = (selected, *auxiliary_sets)
    base_rendered_ids = tuple(
        dict.fromkeys(
            record_id
            for node in nodes
            for record_id in node.record_ids
        )
    )
    table_record_ids = tuple(
        dict.fromkeys(
            record_id
            for node in nodes
            if re.search(r"(?m)^\|\s*-{3,}", node.text)
            for record_id in node.record_ids
        )
    )
    realization = build_narrative_realization(
        rendered_sets,
        base_rendered_ids,
        table_record_ids=table_record_ids,
    )
    has_table_reference = any(
        "아래 정본 표" in node.text for node in realization.nodes
    )
    if has_table_reference != bool(realization.table_reference_record_ids):
        raise LosslessInvariantError("narrative table reference binding mismatch")
    if set(realization.table_reference_record_ids) - set(table_record_ids):
        raise LosslessInvariantError("narrative references records without a rendered table")
    verified_recomputations = tuple(
        verify_recomputation(proof, rendered_sets)
        for proof in realization.recomputations
    )
    if any(not item.matched for item in verified_recomputations):
        raise LosslessInvariantError("narrative relation recomputation mismatch")
    visible_narrative_nodes = tuple(
        node
        for node in realization.nodes
        if node.block_id != "narrative:field-restatement"
    )
    nodes = _insert_narrative_nodes(nodes, visible_narrative_nodes)
    rendered_ids = tuple(
        dict.fromkeys(record_id for node in nodes for record_id in node.record_ids)
    )
    source_ids = {
        record.evidence_id
        for evidence_set in rendered_sets
        for record in evidence_set.records
    }
    unknown = set(rendered_ids) - source_ids
    if unknown:
        raise LosslessInvariantError(
            f"render nodes reference unknown evidence ids: {sorted(unknown)}"
        )
    surfaced = {field for node in nodes for field in node.surface_fields}
    received = sum(item.coverage.records_received for item in rendered_sets)
    total_reported = (
        sum(item.coverage.total_reported or 0 for item in rendered_sets)
        if all(item.coverage.total_reported is not None for item in rendered_sets)
        else None
    )
    coverage = selected.coverage.model_copy(
        update={
            "total_reported": total_reported,
            "records_received": received,
            "records_unique": len(source_ids),
            "records_rendered": len(rendered_ids),
            "pagination_complete": all(
                item.coverage.pagination_complete for item in rendered_sets
            ),
            "partial_reasons": tuple(
                dict.fromkeys(
                    reason
                    for item in rendered_sets
                    for reason in item.coverage.partial_reasons
                )
            ),
        }
    )
    if coverage.records_rendered > coverage.records_received:
        raise LosslessInvariantError("records_rendered cannot exceed records_received")
    record_rate = len(rendered_ids) / len(source_ids) if source_ids else 1.0
    field_rate = len(set(required) & surfaced) / len(required) if required else 1.0
    return DeterministicRender(
        profile=profile,
        text="\n\n".join(node.text for node in nodes if node.text.strip()),
        nodes=tuple(nodes),
        coverage=coverage,
        source_refs=_source_refs(rendered_sets),
        required_fields=required,
        record_surface_rate=round(record_rate, 6),
        required_field_surface_rate=round(field_rate, 6),
        request_notice=_request_satisfaction_notice(plan, evidence_sets),
        source_notices=source_notices,
        source_notice_bindings=source_notice_bindings,
        source_tiers=source_tiers,
        structured_claims=tuple(
            {
                **item.claim.model_dump(mode="json"),
                "surface_text": item.text,
            }
            for item in realization.claims
        ),
        structured_recomputations=tuple(
            item.model_dump(mode="json") for item in verified_recomputations
        ),
        structured_claims_truncated=realization.truncated_t2_count,
        unnarrated_record_count=realization.unnarrated_record_count,
        narrated_record_ids=realization.narrated_record_ids,
        unnarrated_records=realization.unnarrated_records,
        record_field_usage=realization.record_field_usage,
        average_narrated_field_count=realization.average_narrated_field_count,
        loaded_field_narrative_use_rate=realization.loaded_field_narrative_use_rate,
        identifier_only_sentence_count=realization.identifier_only_sentence_count,
    )


def _insert_narrative_nodes(
    nodes: Sequence[RenderNode],
    narrative_nodes: Sequence[RenderNode],
) -> list[RenderNode]:
    coverage = [node for node in nodes if node.block_id.endswith(":coverage")]
    remainder = [node for node in nodes if not node.block_id.endswith(":coverage")]
    return [*coverage, *narrative_nodes, *remainder]


def _insert_auxiliary_nodes(
    plan: PlannerOutput,
    primary_nodes: Sequence[RenderNode],
    auxiliary_sets: Sequence[EvidenceSet],
) -> list[RenderNode]:
    coverage_nodes: list[RenderNode] = []
    primary_fact_nodes: list[RenderNode] = []
    primary_news_nodes: list[RenderNode] = []
    limit_nodes: list[RenderNode] = []
    for node in primary_nodes:
        if node.block_id.endswith(":coverage"):
            coverage_nodes.append(node)
        elif node.block_id.endswith(":news"):
            primary_news_nodes.append(node)
        elif (
            node.block_id.endswith(":limits")
            or node.block_id == "requested-fields:absence"
        ):
            limit_nodes.append(node)
        else:
            primary_fact_nodes.append(node)

    # Auxiliary records remain available to narrative realization and inspection.
    # Their generic status cards are not useful answer content and expose lane mechanics.
    del plan, auxiliary_sets
    return [
        *coverage_nodes,
        *primary_fact_nodes,
        *primary_news_nodes,
        *limit_nodes,
    ]


def _source_refs(
    evidence_sets: Sequence[EvidenceSet],
) -> tuple[SourceReference, ...]:
    refs: dict[str, SourceReference] = {}
    for evidence_set in evidence_sets:
        for ref in evidence_set.source_refs:
            current = refs.get(ref.url)
            if current is None or (not current.title and ref.title):
                refs[ref.url] = ref
    return tuple(refs.values())


def _source_failure_notices(
    evidence_sets: Sequence[EvidenceSet],
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    notices: list[str] = []
    bindings: list[dict[str, Any]] = []
    for evidence_set in evidence_sets:
        label = public_source_label(evidence_set.source)
        for failure in evidence_set.item_failures:
            result = SourceResult(
                source=evidence_set.source,
                query=text(failure.get("query")) or "source retrieval",
                status=_failure_source_status(failure),
                notice=(
                    text(failure.get("notice"))
                    or text(failure.get("summary"))
                    or None
                ),
            )
            event = retrieval_event_from_result(result)
            notice = public_retrieval_notice(event, label=label)
            notices.append(notice)
            bindings.append(
                {
                    "record_id": event.record_id,
                    "notice": notice,
                    "reason_code": event.reason_code,
                    "exposure_layer": event.exposure_layer,
                    "tool": event.tool,
                }
            )
    return tuple(dict.fromkeys(notices)), tuple(bindings)


def _failure_source_status(failure: Mapping[str, Any]) -> str:
    return classify_failure_signals(
        (text(failure.get("status")),),
        " ".join(text(failure.get(key)) for key in ("notice", "summary")),
    )


def _source_notices_enabled(
    plan: PlannerOutput,
    profile: RenderProfile,
    evidence_sets: Sequence[EvidenceSet],
) -> bool:
    if any(
        str(failure.get("status") or "") == "scope_limit"
        for evidence_set in evidence_sets
        for failure in evidence_set.item_failures
    ):
        return True
    if profile != "market_analysis":
        return True
    normalized = plan.resolved_question.casefold()
    return any(
        token in normalized
        for token in (
            "임상",
            "clinical",
            "nct",
            "특허",
            "오렌지북",
            "orange book",
            "급여",
            "보험",
            "reimbursement",
        )
    )


def select_render_profile(
    plan: PlannerOutput,
    evidence_sets: Sequence[EvidenceSet],
) -> RenderProfile:
    if plan.answer_sources == ("mart",):
        return "market_analysis"
    question = plan.resolved_question.casefold()
    clinical = _set_for("clinicaltrials", evidence_sets)
    patent = _set_for("patent", evidence_sets)
    policy = _set_for("hira", evidence_sets)
    if clinical and clinical.records and ("임상" in question or "nct" in question):
        if re.search(r"\bNCT\d{8}\b", plan.resolved_question, re.IGNORECASE):
            return "single_record_detail"
        return "clinical_portfolio"
    if patent and patent.records and "특허" in question:
        return "patent_portfolio"
    if policy and policy.records and any(token in question for token in ("급여", "고시")):
        return "policy_document"
    return "market_analysis"


def _inject_missing_field_node(
    nodes: Sequence[RenderNode],
    evidence_set: EvidenceSet,
    required: tuple[str, ...],
) -> list[RenderNode]:
    del evidence_set, required
    return list(nodes)


def _request_satisfaction_notice(
    plan: PlannerOutput,
    evidence_sets: Sequence[EvidenceSet],
) -> str | None:
    requested = set(plan.requested_answer_shape.measure_or_attribute)
    if "api_unit_price" in requested and not _payload_has_key(
        evidence_sets,
        ("api_unit_price", "unit_price"),
    ):
        return (
            "요청하신 API 단가는 현재 연결된 원천에서 확인되지 않았습니다. "
            "아래 자료는 관련 참고자료이며 요청값의 대체값이 아닙니다."
        )
    if plan.requested_answer_shape.time_horizon == "최근 10년":
        years = _payload_years(evidence_sets)
        if len(years) < 10:
            observed_range = ""
            if years:
                ordered = sorted(years)
                period = (
                    f"{ordered[0]}년"
                    if len(ordered) == 1
                    else f"{ordered[0]}~{ordered[-1]}년"
                )
                observed_range = f"확인된 보유 연도 범위는 {period}이며, "
            return (
                "요청하신 최근 10년 전체 추이는 현재 연결된 원천에서 확인되지 않았습니다. "
                f"{observed_range}아래 확인된 보유 구간 자료는 관련 참고자료이며 "
                "요청값의 대체값이 아닙니다."
            )
    if "active_clinical_trials" in requested:
        clinical = _set_for("clinicaltrials", evidence_sets)
        if clinical is not None and not any(
            _is_active_kr(record.payload) for record in clinical.records
        ):
            return (
                "요청하신 국내 진행 중 임상시험은 현재 연결된 원천에서 확인되지 않았습니다. "
                "아래 인접 임상 자료는 관련 참고자료이며 요청값의 대체값이 아닙니다."
            )
    return None


def _payload_has_key(
    evidence_sets: Sequence[EvidenceSet],
    keys: tuple[str, ...],
) -> bool:
    wanted = {key.casefold() for key in keys}

    def visit(value: object) -> bool:
        if isinstance(value, Mapping):
            return any(
                (str(key).casefold() in wanted and _has_surface_value(item))
                or visit(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(visit(item) for item in value)
        return False

    return any(
        visit(record.payload)
        for evidence_set in evidence_sets
        for record in evidence_set.records
    )


def _has_surface_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_surface_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_surface_value(item) for item in value)
    return True


def _payload_years(evidence_sets: Sequence[EvidenceSet]) -> set[str]:
    years: set[str] = set()
    for evidence_set in evidence_sets:
        for record in evidence_set.records:
            years.update(
                re.findall(
                    r"(?:19|20)\d{2}",
                    json.dumps(record.payload, ensure_ascii=False, default=str),
                )
            )
    return years


def _is_active_kr(payload: Mapping[str, Any]) -> bool:
    status = text(payload.get("overall_status")).upper()
    countries = {
        text(value).casefold()
        for value in payload.get("countries", ())
        if text(value)
    }
    return status in ACTIVE_CLINICAL_STATUSES and any(
        country in {"korea, republic of", "south korea", "대한민국", "한국"}
        for country in countries
    )


def _selected_set(
    profile: RenderProfile,
    evidence_sets: Sequence[EvidenceSet],
) -> EvidenceSet | None:
    source = {
        "clinical_portfolio": "clinicaltrials",
        "single_record_detail": "clinicaltrials",
        "patent_portfolio": "patent",
        "policy_document": "hira",
    }.get(profile)
    return _set_for(source, evidence_sets) if source else None


def _set_for(
    source: str | None,
    evidence_sets: Sequence[EvidenceSet],
) -> EvidenceSet | None:
    return next((item for item in evidence_sets if item.source == source), None)
