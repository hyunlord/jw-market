from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ValueRef:
    value_id: str
    metric_id: str
    canonical_value: str
    canonical_unit: str
    display_policy_id: str


@dataclass(frozen=True, slots=True)
class DisplayValue:
    value_id: str
    display_value: str
    display_unit: str
    display_text: str


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
    text_template: str
    value_refs: tuple[ValueRef, ...]
    evidence_ids: tuple[str, ...]


def claims_for(intent: str, data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    if data.get("contract_id") == "D1":
        return _d1_claims(data)
    builders = {
        "MARKET_SIZE_TREND": _a1_claims,
        "BRAND_TREND": _c1_claims,
        "MARKET_OUTLOOK": _a2_claims,
        "COMPETITION_CHANGE": _b1_claims,
        "COMPETITOR_POSITION": _b2_claims,
        "SOURCE_DIFFERENCE": _c3_claims,
        "CHANNEL_SPECIALTY": _c2_claims,
        "SALES_ACTIVITY_TREND": _d3_claims,
        "SALES_IMPACT": _d2_claims,
        "NEW_ENTRANT_THREAT": _b3_claims,
        "MULTI_SOURCE_SNAPSHOT": _a3_claims,
        "EXTERNAL_LOOKUP": _e1_claims,
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
    values: tuple[ValueRef, ...] = (),
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
        text_template=text,
        value_refs=values,
        evidence_ids=refs,
    )


def value_ref(
    value_id: str,
    metric_id: str,
    value: Any,
    unit: str,
    display_policy_id: str,
) -> ValueRef:
    return ValueRef(value_id, metric_id, str(value), unit, display_policy_id)


def display_value(ref: ValueRef) -> DisplayValue:
    value = Decimal(ref.canonical_value)
    if ref.display_policy_id == "krw_eok_2":
        displayed = (value / Decimal("100000000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        text = f"{displayed:,.2f}억원"
        return DisplayValue(ref.value_id, f"{displayed:,.2f}", "억원", text)
    if ref.display_policy_id == "pct_2":
        displayed = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        text = f"{displayed:,.2f}%"
        return DisplayValue(ref.value_id, f"{displayed:,.2f}", "%", text)
    if ref.display_policy_id == "count_0":
        displayed = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        text = f"{displayed:,.0f}건"
        return DisplayValue(ref.value_id, f"{displayed:,.0f}", "건", text)
    raise ValueError(f"unsupported_display_policy:{ref.display_policy_id}")


def render_claim(claim: AnswerClaim) -> str:
    rendered = claim.text_template
    for ref in claim.value_refs:
        rendered = rendered.replace("{" + ref.value_id + "}", display_value(ref).display_text)
    return rendered


def _refs(data: Mapping[str, Any], default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in data.get("evidence_refs", ()) if str(item)) or default


def _rows(data: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def _number(value: Any) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "미확인"


def _percent_text(value: Any) -> str:
    return f"{value:.2f}%" if isinstance(value, (int, float)) else "미확인"


def _a1_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ("market.series",))
    claims: list[AnswerClaim] = []
    for row in _rows(data, "source_summaries"):
        source = str(row.get("source") or "market source")
        start_period = str(row.get("start_period") or "")
        end_period = str(row.get("end_period") or "")
        period = f"{start_period}~{end_period}" if start_period and end_period else end_period
        latest = row.get("end_market_size_krw")
        if isinstance(latest, (int, float)):
            values = (value_ref("latest", "KRW_MARKET_SIZE", latest, "KRW", "krw_eok_2"),)
            claims.append(_claim("latest_market_size", f"{source} {end_period} 시장 규모 {{latest}}", source, refs, period=end_period, values=values))
        start = row.get("start_market_size_krw")
        if isinstance(start, (int, float)) and isinstance(latest, (int, float)):
            values = (
                value_ref("start", "KRW_MARKET_SIZE", start, "KRW", "krw_eok_2"),
                value_ref("end", "KRW_MARKET_SIZE", latest, "KRW", "krw_eok_2"),
            )
            claims.append(_claim("market_size_trend", f"{source} 시장 규모는 {start_period} {{start}}에서 {end_period} {{end}}으로 변했습니다.", source, refs, period=period, values=values))
    return tuple(claims)


def _c1_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ("market.brand_series",))
    claims: list[AnswerClaim] = []
    for row in _rows(data, "source_results"):
        source = str(row.get("source") or "market source")
        period = str(row.get("period") or "")
        start = row.get("brand_start_sales_krw")
        end = row.get("brand_end_sales_krw")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            values = (
                value_ref("start", "KRW_SALES", start, "KRW", "krw_eok_2"),
                value_ref("end", "KRW_SALES", end, "KRW", "krw_eok_2"),
            )
            claims.append(_claim("brand_sales_series", f"{source} {period} 브랜드 매출 {{start}} → {{end}}", source, refs, period=period, values=values))
        growth = row.get("brand_growth_pct")
        market_growth = row.get("market_growth_pct")
        if isinstance(growth, (int, float)):
            comparison = f", 시장 {_percent_text(market_growth)}" if isinstance(market_growth, (int, float)) else ""
            claims.append(_claim("brand_trend_conclusion", f"브랜드 성장률은 {_percent_text(growth)}{comparison}입니다.", source, refs, period=period))
    return tuple(claims)


def _b2_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ("market.cohort",))
    claims: list[AnswerClaim] = []
    for row in _rows(data, "source_results"):
        source = str(row.get("source") or "market source")
        population = row.get("population")
        score = row.get("cohort_z_score")
        basis = str(row.get("competition_basis") or "")
        if isinstance(population, int) and basis:
            claims.append(_claim("competitor_definition", f"경쟁군은 동일 시장·출처·기간의 {population}개 브랜드입니다.", source, refs))
        if isinstance(score, (int, float)):
            claims.append(_claim("own_position", f"요청 브랜드의 cohort z-score는 {score:.3f}입니다.", source, refs))
        if isinstance(population, int) and isinstance(score, (int, float)):
            claims.append(_claim("competitor_comparison", f"{population}개 브랜드 분포에서 평균 대비 위치를 z-score {score:.3f}로 비교했습니다.", source, refs))
    return tuple(claims)


def _distribution_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    items = sorted((str(name), share) for name, share in value.items() if isinstance(share, (int, float)))
    return ", ".join(f"{name} {share:.2f}%" for name, share in items)


def _c2_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    distributions = data.get("distributions")
    if not isinstance(distributions, Mapping):
        return ()
    refs = _refs(data, ("UBIST.axis_distribution",))
    claims: list[AnswerClaim] = []
    channel = _distribution_text(distributions.get("channel"))
    specialty = _distribution_text(distributions.get("specialty"))
    if channel:
        claims.append(_claim("channel_distribution", f"채널 구성: {channel}", "UBIST", refs))
    if specialty:
        claims.append(_claim("specialty_distribution", f"진료과 구성: {specialty}", "UBIST", refs))
    return tuple(claims)


def _d2_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ("CSD.market_alignment",))
    claims: list[AnswerClaim] = []
    for row in _rows(data, "source_results"):
        source = str(row.get("source") or "market source")
        period = str(row.get("period") or "")
        activity = row.get("activity_change_rate_pct")
        performance = row.get("performance_change_rate_pct")
        if isinstance(activity, (int, float)):
            claims.append(_claim("activity_change", f"CSD {period} 활동 변화율은 {_percent_text(activity)}입니다.", "CSD", refs, period=period))
        if isinstance(performance, (int, float)):
            claims.append(_claim("performance_change", f"{source} {period} 매출 변화율은 {_percent_text(performance)}입니다.", source, refs, period=period))
        if isinstance(activity, (int, float)) and isinstance(performance, (int, float)):
            claims.append(_claim("temporal_alignment", f"두 변화는 {period} 시점 범위에서 나란히 대조했습니다.", f"CSD+{source}", refs, period=period))
            claims.append(_claim("noncausal_limit", "시점이 겹친다는 관측이며 인과를 단정하지 않습니다.", f"CSD+{source}", refs, period=period, claim_type="limitation"))
    return tuple(claims)


def _e1_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ("NEWS.items",))
    rows = sorted(
        _rows(data, "news_refs"),
        key=lambda row: (str(row.get("date") or ""), str(row.get("title") or ""), str(row.get("url") or "")),
        reverse=True,
    )
    if not rows:
        return ()
    source = "NEWS"
    items = "; ".join(
        f"{row.get('title')} ({row.get('date')}, {row.get('source')}) {row.get('url')}"
        for row in rows
    )
    return (
        _claim("capability_level", "현재 확인 가능한 외부 기사 항목을 조회했습니다.", source, refs),
        _claim("selection_basis", "제목·날짜·매체·URL이 모두 확인된 항목만 포함했습니다.", source, refs),
        _claim("result_items", items, source, refs),
    )


def _a2_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    rows = data.get("source_results")
    row = next((item for item in rows if isinstance(item, Mapping)), data) if isinstance(rows, list) else data
    source = str(row.get("source") or "market source")
    period = str(row.get("period") or "")
    refs = _refs(data, (f"{source}.trend",))
    end_period = period.rsplit("~", 1)[-1]
    forecast_period = _next_month(end_period)
    trend = row.get("trend_rate_pct")
    forecast = row.get("forecast_krw")
    if not isinstance(trend, (int, float)) or not isinstance(forecast, (int, float)) or not forecast_period:
        return ()
    trend_ref = value_ref("trend", "MONTHLY_COMPOUND_GROWTH_RATE", trend, "PCT", "pct_2")
    forecast_ref = value_ref("forecast", "KRW_BRAND_MONTHLY_SALES_FORECAST", forecast, "KRW", "krw_eok_2")
    return (
        _claim("recent_observed_trend", f"{source} 리바로 월 매출의 {period} 월 복합성장률은 {{trend}}입니다.", source, refs, period=period, values=(trend_ref,)),
        _claim("forecast_basis", f"예측=추세연장: {end_period} 리바로 월 매출에 같은 월 복합성장률을 1개월 적용한 {forecast_period} 조건부 값은 {{forecast}}입니다.", source, refs, period=f"{end_period}~{forecast_period}", values=(forecast_ref,)),
        _claim("risk_factors", "신규 진입, 약가 변화 등 외부 위험요인은 이 조건부 값에 반영되지 않았습니다.", source, refs, period=period),
        _claim("forecast_availability", "현재 제공 가능한 전망은 리바로 월 매출의 관측 추세를 1개월 연장한 조건부 전망입니다.", source, refs, period=period),
        _claim("uncertainty", f"월별 관측 추세의 단순 연장이므로 실제 값은 달라질 수 있습니다({forecast_period} 전망).", source, refs, period=period),
    )


def _next_month(period: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if match is None:
        return ""
    year, month = (int(part) for part in match.groups())
    return f"{year + (month == 12):04d}-{month % 12 + 1:02d}"


def _d1_claims(data: Mapping[str, Any]) -> tuple[AnswerClaim, ...]:
    refs = _refs(data, ("CSD.render_data.series",))
    rows = _rows(data, "activity_trend")
    if len(rows) < 2:
        return ()
    start_period = str(rows[0].get("period") or "")
    end_period = str(rows[-1].get("period") or "")
    start = rows[0].get("product_details")
    end = rows[-1].get("product_details")
    rate = data.get("activity_change_rate_pct")
    if not all(isinstance(value, (int, float)) for value in (start, end, rate)):
        return ()
    period = str(data.get("period") or f"{start_period}~{end_period}")
    return (
        _claim(
            "activity_series",
            f"CSD TOTAL 영업활동은 {start_period} {{start}}에서 {end_period} {{end}}으로 변했습니다.",
            "CSD",
            refs,
            period=period,
            values=(
                value_ref("start", "CSD_ACTIVITY_COUNT", start, "COUNT", "count_0"),
                value_ref("end", "CSD_ACTIVITY_COUNT", end, "COUNT", "count_0"),
            ),
        ),
        _claim(
            "activity_change",
            "같은 기간 영업활동 변화율은 {rate}입니다.",
            "CSD",
            refs,
            period=period,
            values=(value_ref("rate", "CSD_ACTIVITY_CHANGE_RATE", rate, "PCT", "pct_2"),),
        ),
        _claim("activity_coverage", "CSD TOTAL 판매사 범위의 제품상세 활동 건수 기준입니다.", "CSD", refs, period=period),
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
            values = (value_ref("sales", "KRW_SALES", sales, "KRW", "krw_eok_2"),)
            claims.append(_claim("sales_value", f"{source} {period} 매출 {{sales}}", source, refs or (f"{source}.render_data.brand_value_series_10pt",), period=period, values=values))
    claims.append(_claim("source_separation_limit", "HIRA 환자수와 시장 매출은 모집단과 정의가 같다는 근거가 없어 나란히 표시하며 합산하지 않고 환자당 매출로도 계산하지 않습니다.", "HIRA+market sources", refs or ("source.contracts",), claim_type="limitation"))
    return tuple(claims)
