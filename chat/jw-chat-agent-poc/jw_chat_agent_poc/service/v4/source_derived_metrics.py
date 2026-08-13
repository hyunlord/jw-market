from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.contracts import SourceResult


Formula = Literal["ratio", "percentage", "percentage_point_difference"]


class DerivedMetricProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str
    formula: Formula
    inputs: tuple[Decimal, ...]
    result: Decimal
    unit: str
    matched: bool = True


class HiraDerivedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = ""
    proofs: tuple[DerivedMetricProof, ...] = ()
    scope_notice: str | None = None


@dataclass(frozen=True, slots=True)
class _CareStats:
    care_type: str
    patient_count: Decimal
    total_cost: Decimal
    insurer_cost: Decimal
    claim_count: Decimal
    visit_days: Decimal
    female_patient_count: Decimal | None
    female_visit_days: Decimal | None


def build_hira_derived_outcome(
    results: Sequence[SourceResult],
) -> HiraDerivedOutcome:
    rows, subject, year, scope_notice = _hira_rows(results)
    outpatient = rows.get("외래")
    inpatient = rows.get("입원")
    if outpatient is None or inpatient is None:
        return HiraDerivedOutcome(scope_notice=scope_notice)
    denominators = (
        outpatient.patient_count,
        inpatient.patient_count,
        outpatient.total_cost,
        inpatient.total_cost,
        outpatient.claim_count,
        inpatient.claim_count,
        outpatient.visit_days,
        inpatient.visit_days,
        outpatient.patient_count + inpatient.patient_count,
        outpatient.visit_days + inpatient.visit_days,
        outpatient.total_cost + inpatient.total_cost,
    )
    if any(value == 0 for value in denominators):
        return HiraDerivedOutcome(scope_notice=scope_notice)

    proofs = (
        _proof("outpatient_visits_per_patient", "ratio", outpatient.visit_days, outpatient.patient_count, unit="일"),
        _proof("inpatient_visits_per_patient", "ratio", inpatient.visit_days, inpatient.patient_count, unit="일"),
        _proof("inpatient_outpatient_visit_multiple", "ratio", inpatient.visit_days / inpatient.patient_count, outpatient.visit_days / outpatient.patient_count, unit="배"),
        _proof("outpatient_cost_per_patient", "ratio", outpatient.total_cost, outpatient.patient_count, unit="원"),
        _proof("inpatient_cost_per_patient", "ratio", inpatient.total_cost, inpatient.patient_count, unit="원"),
        _proof("inpatient_outpatient_cost_multiple", "ratio", inpatient.total_cost / inpatient.patient_count, outpatient.total_cost / outpatient.patient_count, unit="배"),
        _proof("outpatient_insurer_burden_rate", "percentage", outpatient.insurer_cost, outpatient.total_cost, unit="%"),
        _proof("inpatient_insurer_burden_rate", "percentage", inpatient.insurer_cost, inpatient.total_cost, unit="%"),
        _proof("insurer_burden_rate_gap", "percentage_point_difference", inpatient.insurer_cost, inpatient.total_cost, outpatient.insurer_cost, outpatient.total_cost, unit="%p"),
        _proof("outpatient_visits_per_claim", "ratio", outpatient.visit_days, outpatient.claim_count, unit="일"),
        _proof("inpatient_visits_per_claim", "ratio", inpatient.visit_days, inpatient.claim_count, unit="일"),
        *_female_share_proofs(outpatient, inpatient),
        _proof("outpatient_patient_share", "percentage", outpatient.patient_count, outpatient.patient_count + inpatient.patient_count, unit="%"),
        _proof("outpatient_visit_share", "percentage", outpatient.visit_days, outpatient.visit_days + inpatient.visit_days, unit="%"),
        _proof("outpatient_cost_share", "percentage", outpatient.total_cost, outpatient.total_cost + inpatient.total_cost, unit="%"),
    )
    verified = tuple(_verify(proof) for proof in proofs)
    if any(not proof.matched for proof in verified):
        return HiraDerivedOutcome(proofs=verified, scope_notice=scope_notice)

    by_id = {proof.metric_id: proof.result for proof in verified}
    text = (
        f"{year}년 {subject} 기준 1인당 방문일수는 외래 "
        f"{_number(by_id['outpatient_visits_per_patient'])}일, 입원 "
        f"{_number(by_id['inpatient_visits_per_patient'])}일이며 입원은 외래의 "
        f"{_number(by_id['inpatient_outpatient_visit_multiple'])}배입니다. "
        f"1인당 요양급여비용은 외래 {_won(by_id['outpatient_cost_per_patient'])}원, "
        f"입원 {_won(by_id['inpatient_cost_per_patient'])}원으로 입원이 "
        f"{_number(by_id['inpatient_outpatient_cost_multiple'])}배입니다. "
        f"보험자부담률은 외래 {_number(by_id['outpatient_insurer_burden_rate'])}%, "
        f"입원 {_number(by_id['inpatient_insurer_burden_rate'])}%로 입원이 "
        f"{_number(by_id['insurer_burden_rate_gap'])}%p 높습니다. "
        f"건당 방문일수는 외래 {_number(by_id['outpatient_visits_per_claim'])}일, "
        f"입원 {_number(by_id['inpatient_visits_per_claim'])}일입니다."
    )
    if "outpatient_female_patient_share" in by_id:
        text += (
            f" 여성 구성비는 환자수 기준 외래 "
            f"{_number(by_id['outpatient_female_patient_share'])}%, 입원 "
            f"{_number(by_id['inpatient_female_patient_share'])}%, 방문일수 기준 외래 "
            f"{_number(by_id['outpatient_female_visit_share'])}%, 입원 "
            f"{_number(by_id['inpatient_female_visit_share'])}%입니다."
        )
    text += (
        f" 외래 비중은 환자수 {_number(by_id['outpatient_patient_share'])}%, "
        f"방문일수 {_number(by_id['outpatient_visit_share'])}%, "
        f"요양급여비용 {_number(by_id['outpatient_cost_share'])}%입니다."
    )
    return HiraDerivedOutcome(text=text, proofs=verified, scope_notice=scope_notice)


def _hira_rows(
    results: Sequence[SourceResult],
) -> tuple[dict[str, _CareStats], str, str, str | None]:
    rows: dict[str, _CareStats] = {}
    subject = "질환"
    year = "해당"
    scope_notice: str | None = None
    for result in results:
        if result.source != "hira" or result.status != "ok":
            continue
        coverage = result.payload.get("period_coverage")
        if isinstance(coverage, Mapping) and coverage.get("requested_axis") == "성별·연령5세구간별":
            scope_notice = "요청하신 성별·연령 5세 구간 조회는 현재 지원되지 않아, 입원/외래 기준으로 답변합니다."
        calls = result.payload.get("calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            render = call.get("render_data")
            if not isinstance(render, Mapping):
                continue
            request = render.get("request")
            if isinstance(request, Mapping):
                subject = str(request.get("sickCd") or subject)
                year = str(request.get("year") or year)
            items = render.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                parsed = _care_stats(item)
                if parsed is not None:
                    rows[parsed.care_type] = parsed
    return rows, subject, year, scope_notice


def _care_stats(value: object) -> _CareStats | None:
    if not isinstance(value, Mapping):
        return None
    care_type = str(value.get("inpatOpat") or "").strip()
    required = tuple(_decimal(value.get(field)) for field in ("ptntCnt", "rvdRpeTamtAmt", "rvdInsupBrdnAmt", "specCnt", "vstDdcnt"))
    if care_type not in {"외래", "입원"} or any(item is None for item in required):
        return None
    patient_count, total_cost, insurer_cost, claim_count, visit_days = (
        item for item in required if item is not None
    )
    sex_rows = value.get("sexBreakdown") or value.get("sexRows")
    female = next(
        (
            row
            for row in sex_rows
            if isinstance(row, Mapping) and str(row.get("sex") or "") in {"여", "여성"}
        ),
        None,
    ) if isinstance(sex_rows, list) else None
    return _CareStats(
        care_type=care_type,
        patient_count=patient_count,
        total_cost=total_cost,
        insurer_cost=insurer_cost,
        claim_count=claim_count,
        visit_days=visit_days,
        female_patient_count=_decimal(female.get("ptntCnt")) if female else None,
        female_visit_days=_decimal(female.get("vstDdcnt")) if female else None,
    )


def _female_share_proofs(
    outpatient: _CareStats,
    inpatient: _CareStats,
) -> tuple[DerivedMetricProof, ...]:
    values = (
        outpatient.female_patient_count,
        inpatient.female_patient_count,
        outpatient.female_visit_days,
        inpatient.female_visit_days,
    )
    if any(value is None for value in values):
        return ()
    female_outpatients, female_inpatients, female_outpatient_visits, female_inpatient_visits = (
        value for value in values if value is not None
    )
    return (
        _proof("outpatient_female_patient_share", "percentage", female_outpatients, outpatient.patient_count, unit="%"),
        _proof("inpatient_female_patient_share", "percentage", female_inpatients, inpatient.patient_count, unit="%"),
        _proof("outpatient_female_visit_share", "percentage", female_outpatient_visits, outpatient.visit_days, unit="%"),
        _proof("inpatient_female_visit_share", "percentage", female_inpatient_visits, inpatient.visit_days, unit="%"),
    )


def _proof(metric_id: str, formula: Formula, *inputs: Decimal, unit: str) -> DerivedMetricProof:
    return DerivedMetricProof(metric_id=metric_id, formula=formula, inputs=inputs, result=_compute(formula, inputs), unit=unit)


def _verify(proof: DerivedMetricProof) -> DerivedMetricProof:
    return proof.model_copy(update={"matched": _compute(proof.formula, proof.inputs) == proof.result})


def _compute(formula: Formula, inputs: Sequence[Decimal]) -> Decimal:
    if formula == "percentage_point_difference":
        return (inputs[0] / inputs[1] - inputs[2] / inputs[3]) * Decimal(100)
    value = inputs[0] / inputs[1]
    return value * Decimal(100) if formula == "percentage" else value


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _number(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _won(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,.0f}"
