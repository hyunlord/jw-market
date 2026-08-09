from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from jw_chat_agent_poc.agent_loop.question_contracts import SLOT_SPECS, QuestionSpec, question_spec_for
from jw_chat_agent_poc.orchestrator.answer_claim_adapters import AnswerClaim, claims_for, render_claim


class AnswerGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimPlan:
    slot_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerFailure:
    code: str
    subject: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerLimitation:
    code: str
    slot_id: str
    reason: str
    display_text: str


@dataclass(frozen=True, slots=True)
class FinalizedAnswer:
    answer: str
    claims: tuple[AnswerClaim, ...]
    limitations: tuple[AnswerLimitation, ...]
    degraded: bool
    answer_status: str
    selected_branch: str


@dataclass(frozen=True, slots=True)
class ControlLayerResult:
    answer: str
    applied: bool
    degraded: bool
    answer_status: str
    intent: str
    required_slot_coverage: str
    claim_plan_hash_input: tuple[str, ...]
    question_spec_sha256: str
    claim_plan_sha256: str
    evidence_set_sha256: str
    evidence_ids: tuple[str, ...]
    source_labels: tuple[str, ...]
    filled_slots: tuple[str, ...]
    selected_branch: str


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _spec_hash(spec: QuestionSpec) -> str:
    return _stable_hash(
        {
            "anchor_provenance": spec.anchor_provenance,
            "forbidden_claim_types": spec.forbidden_claim_types,
            "intent": spec.intent.value,
            "operation_mode": spec.operation_mode.value,
            "optional_slots": spec.optional_slots,
            "blocking_required_slots": spec.blocking_required_slots,
            "partial_required_slots": spec.partial_required_slots,
        }
    )


def _passthrough_result(spec: QuestionSpec, answer: str) -> ControlLayerResult:
    empty_hash = _stable_hash(())
    return ControlLayerResult(
        answer=answer,
        applied=False,
        degraded=False,
        answer_status="complete",
        intent=spec.intent.value,
        required_slot_coverage="not_applied",
        claim_plan_hash_input=(),
        question_spec_sha256=_spec_hash(spec),
        claim_plan_sha256=empty_hash,
        evidence_set_sha256=empty_hash,
        evidence_ids=(),
        source_labels=(),
        filled_slots=(),
        selected_branch="passthrough",
    )


def _missing_limitations(spec: QuestionSpec, filled: frozenset[str]) -> tuple[AnswerLimitation, ...]:
    return tuple(
        AnswerLimitation(
            code="required_slot_unfilled",
            slot_id=slot_id,
            reason="NO_ROWS",
            display_text=f"{spec.slot_spec(slot_id).missing_label}: 현재 근거로 확인하지 못했습니다.",
        )
        for slot_id in spec.required_slots
        if slot_id not in filled
    )


def finalize_answer(
    question_spec: QuestionSpec,
    claim_plan: ClaimPlan,
    claims: tuple[AnswerClaim, ...],
    failures: tuple[AnswerFailure, ...],
    *,
    selected_branch: str = "v3",
    degradation_notice: str | None = None,
) -> FinalizedAnswer:
    if not question_spec.anchor_provenance:
        raise AnswerGateError("anchor_provenance_missing")

    entitled = question_spec.entitled_slots
    planned = frozenset(claim_plan.slot_ids)
    for claim in claims:
        if (
            claim.slot_id not in entitled
            or claim.slot_id not in planned
            or claim.claim_type in question_spec.forbidden_claim_types
        ):
            raise AnswerGateError(f"unentitled_claim:{claim.claim_id}")
        if not claim.evidence_ids or not claim.source:
            raise AnswerGateError(f"anchor_provenance_missing:{claim.claim_id}")

    evidence_ids = {evidence_id for claim in claims for evidence_id in claim.evidence_ids}
    if any(failure.evidence_ids and evidence_ids.intersection(failure.evidence_ids) for failure in failures):
        raise AnswerGateError("fallback_reason_inconsistent")

    filled = frozenset(claim.slot_id for claim in claims)
    limitations = _missing_limitations(question_spec, filled)
    missing_blocking = frozenset(question_spec.blocking_required_slots) - filled
    missing_partial = frozenset(question_spec.partial_required_slots) - filled
    if missing_blocking or (question_spec.required_slots and not filled):
        answer_status = "unsupported"
    elif missing_partial or failures or any(claim.claim_type == "limitation" for claim in claims):
        answer_status = "partial"
    else:
        answer_status = "complete"
    degraded = answer_status != "complete" or selected_branch != "v3"
    if selected_branch != "v3" and not degradation_notice:
        raise AnswerGateError("silent_degradation")

    return FinalizedAnswer(
        answer="\n\n".join(render_claim(claim) for claim in claims),
        claims=claims,
        limitations=limitations,
        degraded=degraded,
        answer_status=answer_status,
        selected_branch=selected_branch,
    )


def _analysis_data(result: Mapping[str, Any], contract_id: str) -> Mapping[str, Any] | None:
    calls = result.get("tool_calls")
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        return _d1_fallback_data(result) if contract_id == "D1" else None
    for call in calls:
        if not isinstance(call, Mapping) or call.get("tool") != "bq_analysis":
            continue
        data = call.get("render_data")
        if isinstance(data, Mapping) and data.get("contract_id") == contract_id:
            return data
    if contract_id == "C2":
        return _c2_fallback_data(calls)
    if contract_id == "D1":
        return _d1_fallback_data(result)
    return None


_D1_AGGREGATE_PATTERN = re.compile(
    r"CSD ChannelDynamics aggregate 콜수/활동량\s+"
    r"(?P<start_period>\d{4}-\d{2})\s+(?P<start>[\d,]+)건\s*→\s*"
    r"(?P<end_period>\d{4}-\d{2})\s+(?P<end>[\d,]+)건"
)


def _d1_fallback_data(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    markdown_response = result.get("markdown_response")
    if not isinstance(markdown_response, Mapping):
        return None
    fact_md = str(markdown_response.get("fact_md") or "")
    match = _D1_AGGREGATE_PATTERN.search(fact_md)
    if match is None:
        return None
    start = int(match.group("start").replace(",", ""))
    end = int(match.group("end").replace(",", ""))
    rate = (end - start) / start * 100 if start else 0.0
    start_period = match.group("start_period")
    end_period = match.group("end_period")
    return {
        "contract_id": "D1",
        "period": f"{start_period}~{end_period}",
        "activity_trend": [
            {"period": start_period, "product_details": start},
            {"period": end_period, "product_details": end},
        ],
        "activity_change_rate_pct": rate,
        "region": "TOTAL",
        "evidence_refs": ["CSD.fact_md.aggregate"],
    }


_CHANNEL_NAMES = frozenset({"상급종병", "종병", "병원", "의원", "보건소", "기타"})


def _c2_fallback_data(calls: Sequence[Any]) -> Mapping[str, Any] | None:
    distributions: dict[str, dict[str, float]] = {}
    evidence_refs: set[str] = set()
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        for data in _render_mappings(call.get("render_data")):
            rows = data.get("level_segments")
            if not isinstance(rows, list):
                continue
            values: list[tuple[str, Decimal]] = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                name = str(row.get("name") or "").strip()
                try:
                    value = Decimal(str(row.get("value")))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if name and value >= 0:
                    values.append((name, value))
            total = sum((value for _, value in values), Decimal("0"))
            if not values or total <= 0:
                continue
            requested = str(data.get("requested_dimension") or "").casefold()
            names = {name for name, _ in values}
            dimension = requested if requested in {"channel", "specialty"} else "channel" if names <= _CHANNEL_NAMES else "specialty"
            distributions[dimension] = {name: float(value / total * 100) for name, value in values}
            refs = data.get("evidence_refs")
            if isinstance(refs, list):
                evidence_refs.update(str(item) for item in refs if str(item))
            evidence_refs.add(f"UBIST.{dimension}.level_segments")
    if not distributions:
        return None
    return {
        "contract_id": "C2",
        "distributions": distributions,
        "evidence_refs": sorted(evidence_refs),
    }


def _render_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    found = [value]
    for child in value.values():
        if isinstance(child, Mapping):
            found.extend(_render_mappings(child))
    return tuple(found)


def _controlled_result(
    spec: QuestionSpec,
    claims: tuple[AnswerClaim, ...],
) -> ControlLayerResult:
    plan = ClaimPlan(tuple(claim.slot_id for claim in claims))
    finalized = finalize_answer(spec, plan, claims, ())
    rendered_claims = tuple((spec.slot_spec(claim.slot_id).user_label, render_claim(claim)) for claim in claims)
    table = ""
    answer_body = finalized.answer
    if spec.intent.value == "EXTERNAL_LOOKUP":
        internal = [text for slot, text in rendered_claims if slot == "내부 정형 지표"]
        news = [text for slot, text in rendered_claims if slot != "내부 정형 지표"]
        answer_body = "\n\n".join(
            block
            for block in (
                "\n\n".join(("## 내부 정형 지표", *internal)) if internal else "",
                "\n\n".join(("## 뉴스·외부 이슈", *news)) if news else "",
            )
            if block
        )
    elif rendered_claims:
        table = "\n".join((
            "## 핵심 결과",
            "| 항목 | 내용 |",
            "| --- | --- |",
            *(f"| {_escape_cell(label)} | {_escape_cell(text)} |" for label, text in rendered_claims),
        ))
    limitation_text = "\n".join(f"- {item.display_text} (사유: {item.reason})" for item in finalized.limitations)
    limitation_block = f"## 조회 상태·제한\n{limitation_text}" if limitation_text else ""
    source_rows = tuple(sorted({(claim.source, _claim_period(claim)) for claim in claims if claim.source}))
    source_block = ""
    if source_rows:
        source_block = "\n".join((
            "## 출처",
            "| 출처 | 기준 기간 |",
            "| --- | --- |",
            *(f"| {_escape_cell(source)} | {_escape_cell(period or '해당 조회')} |" for source, period in source_rows),
        ))
    answer = "\n\n".join(block for block in (answer_body, table, limitation_block, source_block) if block)
    evidence_ids = tuple(sorted({evidence_id for claim in claims for evidence_id in claim.evidence_ids}))
    source_labels = tuple(sorted({claim.source for claim in claims if claim.source}))
    return ControlLayerResult(
        answer=answer,
        applied=True,
        degraded=finalized.degraded,
        answer_status=finalized.answer_status,
        intent=spec.intent.value,
        required_slot_coverage=f"{len(spec.required_slots) - len(finalized.limitations)}/{len(spec.required_slots)}",
        claim_plan_hash_input=plan.slot_ids,
        question_spec_sha256=_spec_hash(spec),
        claim_plan_sha256=_stable_hash(plan.slot_ids),
        evidence_set_sha256=_stable_hash(evidence_ids),
        evidence_ids=evidence_ids,
        source_labels=source_labels,
        filled_slots=tuple(sorted({claim.slot_id for claim in claims})),
        selected_branch="answer_projection",
    )


def _claim_period(claim: AnswerClaim) -> str:
    if claim.period_start == claim.period_end:
        return claim.period_start
    return f"{claim.period_start}~{claim.period_end}".strip("~")


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def apply_answer_control_layer(
    question: str,
    result: Mapping[str, Any],
    fallback_answer: str,
) -> ControlLayerResult:
    spec = question_spec_for(question)
    if result.get("context_scope") == "MIXED":
        return _passthrough_result(spec, fallback_answer)
    target_intents = frozenset({
        "MARKET_SIZE_TREND",
        "BRAND_TREND",
        "COMPETITOR_POSITION",
        "CHANNEL_SPECIALTY",
        "SALES_IMPACT",
        "MULTI_SOURCE_SNAPSHOT",
        "EXTERNAL_LOOKUP",
        "SALES_ACTIVITY_TREND",
    })
    contract_id = {
        "MARKET_SIZE_TREND": "A1",
        "BRAND_TREND": "C1",
        "MARKET_OUTLOOK": "A2",
        "COMPETITION_CHANGE": "B1",
        "COMPETITOR_POSITION": "B2",
        "SOURCE_DIFFERENCE": "C3",
        "CHANNEL_SPECIALTY": "C2",
        "SALES_ACTIVITY_TREND": "D3" if "경쟁사" in question else "D1",
        "SALES_IMPACT": "D2",
        "NEW_ENTRANT_THREAT": "B3",
        "MULTI_SOURCE_SNAPSHOT": "A3",
        "EXTERNAL_LOOKUP": "E1",
    }.get(spec.intent.value)
    if contract_id is None:
        return _passthrough_result(spec, fallback_answer)
    data = _analysis_data(result, contract_id)
    if data is None:
        metrics = result.get("agent_loop_metrics")
        attempted_contract = (
            metrics.get("deterministic_plan_kind")
            if isinstance(metrics, Mapping)
            else None
        )
        target_requires_control = (
            spec.intent.value in target_intents
            and (
                attempted_contract == f"BQ:{contract_id}"
                or spec.intent.value == "MULTI_SOURCE_SNAPSHOT"
            )
        )
        if result.get("answer_control_required") is True or target_requires_control:
            return _controlled_result(spec, ())
        deterministic_fallbacks: dict[str, Mapping[str, Any]] = {
            "SOURCE_DIFFERENCE": {},
            "SALES_ACTIVITY_TREND": {
                "status": "unsupported_axis",
                "insights": ["현재 CSD 도구는 경쟁사별 활동 변화를 지원하지 않습니다."],
                "evidence_refs": ["CSD.coverage"],
            },
            "NEW_ENTRANT_THREAT": {
                "launch_acceleration_status": "unsupported_missing_launch_date",
                "evidence_refs": ["market.coverage"],
            },
        }
        fallback_data = (
            deterministic_fallbacks.get(spec.intent.value)
            if contract_id != "D1"
            else None
        )
        if fallback_data is None:
            return _controlled_result(spec, ()) if contract_id == "D1" else _passthrough_result(spec, fallback_answer)
        fallback_claims = claims_for(spec.intent.value, fallback_data)
        return _controlled_result(spec, fallback_claims)
    return _controlled_result(spec, claims_for(spec.intent.value, data))


_CHART_TITLE_POLICY: dict[str, tuple[str, ...]] = {
    "MARKET_SIZE_TREND": ("시장 매출 추이", "시장 규모 추이"),
    "BRAND_TREND": ("리바로 매출 추이", "브랜드 매출 추이"),
    "MARKET_OUTLOOK": ("리바로 매출 추이", "브랜드 매출 추이"),
    "COMPETITION_CHANGE": ("점유율 추이",),
    "COMPETITOR_POSITION": ("브랜드별 점유율", "Brand별 점유율"),
    "CHANNEL_SPECIALTY": ("channel별", "채널별", "specialty별", "진료과별"),
    "SALES_ACTIVITY_TREND": ("CSD TOTAL 활동 추이",),
    "SALES_IMPACT": ("CSD", "매출 추이"),
}


def entitled_charts(
    controlled: ControlLayerResult,
    charts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not controlled.applied:
        return [dict(chart) for chart in charts]
    allowed_titles = _CHART_TITLE_POLICY.get(controlled.intent, ())
    evidence = frozenset(controlled.evidence_ids)
    filled = frozenset(controlled.filled_slots)
    result: list[dict[str, Any]] = []
    for chart in charts:
        title = str(chart.get("title") or "")
        refs = chart.get("evidence_refs")
        chart_refs = frozenset(str(item) for item in refs if str(item)) if isinstance(refs, list) else frozenset()
        if not any(token in title for token in allowed_titles) or not chart_refs or not chart_refs.intersection(evidence):
            continue
        if "channel" in title.casefold() and "channel_distribution" not in filled:
            continue
        if any(token in title for token in ("specialty", "진료과")) and "specialty_distribution" not in filled:
            continue
        result.append(dict(chart))
        if len(result) == 2:
            break
    return result


def validate_controlled_surface(
    *,
    answer: str,
    charts: Sequence[Mapping[str, Any]],
    sources: Sequence[str],
    answer_status: str,
    allowed_evidence_ids: Sequence[str],
    source_block_required: bool,
) -> None:
    serialized_charts = json.dumps(list(charts), ensure_ascii=False, sort_keys=True)
    surface = f"{answer}\n{serialized_charts}"
    exposed = next((slot_id for slot_id in SLOT_SPECS if re.search(rf"(?<![A-Za-z0-9_]){re.escape(slot_id)}(?![A-Za-z0-9_])", surface)), None)
    if exposed is not None:
        raise AnswerGateError(f"internal_identifier_exposed:{exposed}")
    if re.search(r"(?<![\d,])\d{7,}(?:\.\d+)?원", surface.replace(",", "")):
        raise AnswerGateError("raw_canonical_numeric_exposed")
    chart_text = "\n".join(_chart_presentation_text(charts))
    normalized_chart_text = chart_text.replace(",", "")
    if re.search(r"(?<!\d)(?:\d{7,}\.\d+|\d{10,})(?!\d)", normalized_chart_text):
        raise AnswerGateError("raw_canonical_numeric_exposed")
    if answer_status not in {"complete", "partial", "unsupported"}:
        raise AnswerGateError("answer_status_slot_inconsistent")
    has_source_block = re.search(r"(?m)^## 출처[ \t]*\n\| 출처 \|", answer) is not None
    if source_block_required and (not sources or not has_source_block):
        raise AnswerGateError("required_source_block_missing")
    allowed = frozenset(allowed_evidence_ids)
    for chart in charts:
        refs = chart.get("evidence_refs")
        chart_refs = frozenset(str(item) for item in refs if str(item)) if isinstance(refs, list) else frozenset()
        if not chart_refs or not chart_refs.issubset(allowed):
            raise AnswerGateError("chart_evidence_unentitled")
    if answer_status != "unsupported" and "| 항목 | 내용 |" not in answer:
        raise AnswerGateError("required_artifact_missing")


def _chart_presentation_text(value: Any, *, field_name: str = "") -> tuple[str, ...]:
    presentation_fields = frozenset({"title", "subtitle", "label", "tooltip", "value_label", "formatted_value"})
    if isinstance(value, Mapping):
        items: list[str] = []
        for key, item in value.items():
            items.extend(_chart_presentation_text(item, field_name=str(key).casefold()))
        return tuple(items)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(text for item in value for text in _chart_presentation_text(item, field_name=field_name))
    if field_name in presentation_fields and isinstance(value, str):
        return (value,)
    return ()
