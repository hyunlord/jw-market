from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import json
import re
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    EvidenceSet,
    LosslessInvariantError,
    RenderNode,
    RenderProfile,
)
from jw_chat_agent_poc.service.v4.render_clinical import (
    ACTIVE_CLINICAL_STATUSES,
    render_clinical,
)
from jw_chat_agent_poc.service.v4.render_common import text
from jw_chat_agent_poc.service.v4.render_patent import render_patent
from jw_chat_agent_poc.service.v4.render_policy import render_policy


def render_deterministic_facts(
    plan: PlannerOutput,
    evidence_sets: Sequence[EvidenceSet],
    *,
    observed_on: date,
) -> DeterministicRender:
    profile = select_render_profile(plan, evidence_sets)
    if profile == "market_analysis":
        return DeterministicRender(
            profile=profile,
            request_notice=_request_satisfaction_notice(plan, evidence_sets),
        )

    selected = _selected_set(profile, evidence_sets)
    if selected is None:
        return DeterministicRender(profile="market_analysis")
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
    rendered_ids = tuple(
        dict.fromkeys(record_id for node in nodes for record_id in node.record_ids)
    )
    source_ids = {record.evidence_id for record in selected.records}
    unknown = set(rendered_ids) - source_ids
    if unknown:
        raise LosslessInvariantError(
            f"render nodes reference unknown evidence ids: {sorted(unknown)}"
        )
    surfaced = {field for node in nodes for field in node.surface_fields}
    coverage = selected.coverage.model_copy(update={"records_rendered": len(rendered_ids)})
    if coverage.records_rendered > coverage.records_received:
        raise LosslessInvariantError("records_rendered cannot exceed records_received")
    record_rate = len(rendered_ids) / len(source_ids) if source_ids else 1.0
    field_rate = len(set(required) & surfaced) / len(required) if required else 1.0
    return DeterministicRender(
        profile=profile,
        text="\n\n".join(node.text for node in nodes if node.text.strip()),
        nodes=tuple(nodes),
        coverage=coverage,
        source_refs=selected.source_refs,
        required_fields=required,
        record_surface_rate=round(record_rate, 6),
        required_field_surface_rate=round(field_rate, 6),
        request_notice=_request_satisfaction_notice(plan, evidence_sets),
    )


def select_render_profile(
    plan: PlannerOutput,
    evidence_sets: Sequence[EvidenceSet],
) -> RenderProfile:
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
    output = list(nodes)
    surfaced = {field for node in nodes for field in node.surface_fields}
    missing = [field for field in required if field not in surfaced]
    if missing:
        output.append(
            RenderNode(
                block_id="requested-fields:absence",
                record_ids=tuple(record.evidence_id for record in evidence_set.records),
                surface_fields=tuple(missing),
                text="## 요청 필드 보강\n"
                + "\n".join(f"- {field}: 원천 미제공" for field in missing),
            )
        )
    return output


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
