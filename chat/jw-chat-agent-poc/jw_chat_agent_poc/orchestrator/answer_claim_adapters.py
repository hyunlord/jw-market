from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    claim_id: str
    slot_id: str
    claim_type: str
    subject_type: str
    subject_id: str
    market_scope: str
    source: str
    period_start: str
    period_end: str
    canonical_value: str
    canonical_unit: str
    display_value: str
    display_unit: str
    display_text: str
    evidence_ids: tuple[str, ...]


def claims_for(intent: str, data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    builders = {
        "MARKET_OUTLOOK": _a2_claims,
        "COMPETITION_CHANGE": _b1_claims,
        "SOURCE_DIFFERENCE": _c3_claims,
        "SALES_ACTIVITY_TREND": _d3_claims,
        "NEW_ENTRANT_THREAT": _b3_claims,
        "MULTI_SOURCE_SNAPSHOT": _a3_claims,
    }
    builder = builders.get(intent)
    return builder(data) if builder is not None else ()


def _claim(
    slot_id: str,
    text: str,
    source: str,
    refs: tuple[str, ...],
    *,
    period: str = "",
    claim_type: str = "observation",
) -> AnswerClaim:
    return AnswerClaim(
        claim_id=f"answer-control:{slot_id}",
        slot_id=slot_id,
        claim_type=claim_type,
        subject_type="market",
        subject_id="requested_scope",
        market_scope="STRATEGIC",
        source=source,
        period_start=period,
        period_end=period,
        canonical_value="",
        canonical_unit="",
        display_value="",
        display_unit="",
        display_text=text,
        evidence_ids=refs,
    )


def _refs(data: Mapping[str, Any], default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in data.get("evidence_refs", ()) if str(item)) or default


def _a2_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    rows = data.get("source_results")
    row = next((item for item in rows if isinstance(item, Mapping)), data) if isinstance(rows, list) else data
    source = str(row.get("source") or "market source")
    period = str(row.get("period") or "")
    refs = _refs(data, (f"{source}.trend",))
    uncertainty = str(row.get("forecast_uncertainty_note") or "실제 값은 달라질 수 있습니다.")
    return (
        _claim("recent_observed_trend", f"{source} {period} 관측 추세는 {row.get('trend_rate_pct')}%입니다.", source, refs, period=period),
        _claim("forecast_basis", f"예측=추세연장: 관측 추세를 다음 기간에 단순 연장한 조건부 값은 {row.get('forecast_krw')}원입니다.", source, refs, period=period),
        _claim("risk_factors", "신규 진입, 약가 변화 등 외부 위험요인은 이 조건부 값에 반영되지 않았습니다.", source, refs, period=period),
        _claim("forecast_availability", "현재 제공 가능한 전망은 관측 추세를 연장한 조건부 전망입니다.", source, refs, period=period),
        _claim("uncertainty", uncertainty, source, refs, period=period),
    )


def _c3_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    period = str(data.get("period") or "")
    refs = _refs(data, ("UBIST.contract", "IQVIA_NSA.contract"))
    source = "UBIST+IQVIA_NSA"
    return (
        _claim("measurement_subject_difference", "UBIST와 IQVIA NSA는 측정 대상과 포함 범위가 같은 지표가 아닙니다.", source, refs, period=period),
        _claim("distribution_stage_difference", "두 소스는 유통 단계와 포착 채널이 달라 값의 수준이 달라질 수 있습니다.", source, refs, period=period),
        _claim("cadence_difference", "UBIST는 월 단위, IQVIA NSA는 분기 단위 계약이므로 기준 주기와 최신 시점도 맞춰야 합니다.", source, refs, period=period),
        _claim("direct_comparison_limit", "정의와 주기를 정렬하지 않은 직접 합산 또는 단순 차이 해석은 지원하지 않습니다.", source, refs, period=period),
    )


def _d3_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    insights = data.get("insights")
    insight = next((str(item) for item in insights if str(item).strip()), "판매사별 활동 변화는 현재 미지원입니다.") if isinstance(insights, list) else "판매사별 활동 변화는 현재 미지원입니다."
    refs = _refs(data, ("CSD.coverage",))
    if data.get("status") == "unsupported_axis":
        return (_claim("coverage_and_missingness", insight, "CSD", refs, claim_type="limitation"),)
    period = str(data.get("period") or "")
    return (
        _claim("competitor_activity_change", insight, "CSD", refs, period=period),
        _claim("comparison_period", f"비교기간: {period}", "CSD", refs, period=period),
        _claim("coverage_and_missingness", "CSD TOTAL 판매사 범위에서 확인한 결과입니다.", "CSD", refs, period=period),
    )


def _b3_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    if data.get("launch_acceleration_status") != "unsupported_missing_launch_date":
        return ()
    return (
        _claim(
            "new_observation_basis",
            "현재 데이터에서 신규 관찰 시점을 판별하는 기능은 제공하지 않습니다.",
            "market source",
            _refs(data, ("market.coverage",)),
            claim_type="limitation",
        ),
    )


def _format_top(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    return ", ".join(
        f"{row.get('rank')}위 {row.get('brand')}"
        for row in rows
        if isinstance(row, Mapping) and row.get("rank") is not None and row.get("brand")
    )


def _format_changes(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("brand"):
            continue
        delta = row.get("share_delta_pctp")
        rendered = f"{float(delta):+.2f}%p" if isinstance(delta, (int, float)) else "변화값 미확인"
        parts.append(f"{row['brand']} {rendered}")
    return ", ".join(parts)


def _b1_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    rows = data.get("source_results")
    primary = next((row for row in rows if isinstance(row, Mapping)), data) if isinstance(rows, list) else data
    source = str(primary.get("source") or data.get("source") or "market source")
    period = str(primary.get("period") or data.get("period") or "")
    refs = _refs(data, (f"{source}.level_top5_trend_series",))
    top = _format_top(primary.get("current_top_structure") or data.get("current_top_structure"))
    gainers = _format_changes(primary.get("share_gainers") or data.get("share_gainers"))
    losers = _format_changes(primary.get("share_losers") or data.get("share_losers"))
    candidates = (
        ("comparison_period", f"비교기간: {period}" if period else ""),
        ("current_top_structure", f"현재 상위 구조: {top}" if top else ""),
        ("share_gainers", f"상승: {gainers}" if gainers else ""),
        ("share_losers", f"하락: {losers}" if losers else ""),
        ("competition_change_conclusion", str(primary.get("competition_change_conclusion") or data.get("competition_change_conclusion") or "")),
    )
    return tuple(_claim(slot, text, source, refs, period=period) for slot, text in candidates if text)


def _a3_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ())
    patient_period = str(data.get("patient_period") or "")
    claims: list[AnswerClaim] = []
    patient_count = data.get("patient_count")
    if isinstance(patient_count, (int, float)):
        claims.append(_claim("patient_count", f"HIRA {patient_period} 환자수 {patient_count:,.0f}명", "HIRA", refs or ("HIRA.render_data.items.ptntCnt",), period=patient_period))
    rows = data.get("source_results")
    if isinstance(rows, list):
        for row in rows:
            sales = row.get("sales_krw") if isinstance(row, Mapping) else None
            if not isinstance(sales, (int, float)):
                continue
            source = str(row.get("source") or "market source")
            period = str(row.get("period") or "")
            claims.append(_claim("sales_value", f"{source} {period} 매출 {sales:,.0f}원", source, refs or (f"{source}.render_data.brand_value_series_10pt",), period=period))
    claims.append(_claim("source_separation_limit", "HIRA 환자수와 시장 매출은 모집단과 정의가 같다는 근거가 없어 나란히 표시하며 합산하지 않고 환자당 매출로도 계산하지 않습니다.", "HIRA+market sources", refs or ("source.contracts",), claim_type="limitation"))
    return tuple(claims)
