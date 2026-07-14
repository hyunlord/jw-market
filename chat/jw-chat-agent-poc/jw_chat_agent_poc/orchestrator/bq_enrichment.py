from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.bq_calculations import (
    ChangeOperands,
    aligned_activity_performance_changes,
    calculate_cagr,
    conditional_trend_forecast,
    patient_sales_ratio,
    source_divergence,
)
from jw_chat_agent_poc.orchestrator.bq_market_analysis import build_market_analysis_call
from jw_chat_agent_poc.orchestrator.bq_evidence_ledger import finalize_bq_analysis_call

Call = dict[str, Any]
Builder = Callable[[list[Call]], Call | None]


def build_bq_analysis_call(contract_id: str, calls: list[Call]) -> Call | None:
    builder = _BUILDERS.get(contract_id)
    call = builder(calls) if builder is not None else build_market_analysis_call(contract_id, calls)
    return finalize_bq_analysis_call(call, calls)


def _source_divergence_call(calls: list[Call]) -> Call | None:
    by_source = {source: call for call in calls if (source := _market_source(call))}
    ubist = by_source.get("ubist")
    iqvia = by_source.get("iqvia_nsa")
    if ubist is None or iqvia is None:
        return None
    ubist_period, ubist_value = _period_and_latest(ubist)
    iqvia_period, iqvia_value = _period_and_latest(iqvia)
    if ubist_period != iqvia_period:
        insight = (
            f"UBIST({ubist_period})와 IQVIA NSA({iqvia_period})의 기준기간이 달라 "
            "출처 간 차이를 계산하지 않고 나란히 표시합니다."
        )
        return _analysis_call(
            "C3", "source_divergence", [insight],
            status="incompatible_periods", never_aggregate_sources=True,
            source_labels=["UBIST", "IQVIA NSA"],
        )
    result = source_divergence(primary=iqvia_value, comparison=ubist_value)
    if result.absolute_delta is None:
        return None
    insight = (
        f"{iqvia_period} IQVIA NSA와 UBIST의 매출 차이는 "
        f"{_eok(result.absolute_delta)}억원({_pct(result.relative_delta)})이며 두 출처는 합산하지 않습니다."
    )
    return _analysis_call(
        "C3", "source_divergence", [insight],
        period=iqvia_period,
        ubist_sales_krw=_float(ubist_value),
        iqvia_sales_krw=_float(iqvia_value),
        absolute_delta_krw=_float(result.absolute_delta),
        relative_delta_pct=_percent(result.relative_delta),
        never_aggregate_sources=True,
        source_labels=["UBIST", "IQVIA NSA"],
    )


def _activity_alignment_call(calls: list[Call]) -> Call | None:
    activity = next((call for call in calls if call.get("tool") == "csd_activity_trend"), None)
    results = [
        result
        for market in _market_calls(calls)
        if activity is not None and (result := _activity_source_result(activity, market))
    ]
    if not results:
        return None
    primary = results[0]
    charts = [result.pop("chart") for result in results]
    insights = [str(result.pop("insight")) for result in results]
    return _analysis_call(
        "D2", "activity_performance_alignment", insights,
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=["CSD", *[result["source"] for result in results]],
        never_aggregate_sources=True, temporal_overlap_not_causation=True,
        chart_payloads=charts,
    )


def _activity_source_result(activity: Call, market: Call) -> dict[str, Any] | None:
    activity_series = _series(activity, "series", "product_details")
    market_series = _series(market, "brand_value_series_10pt", "value_krw")
    labels = [period for period in activity_series if period in market_series]
    valid = [period for period in labels if activity_series[period] is not None and market_series[period] is not None]
    if len(valid) < 2:
        return None
    start, end = valid[0], valid[-1]
    changes = aligned_activity_performance_changes(
        activity=ChangeOperands(activity_series[start], activity_series[end]),
        performance=ChangeOperands(market_series[start], market_series[end]),
    )
    source = _source_label(market)
    direction = "같은 방향으로 변한" if _same_direction(changes.activity_delta, changes.performance_delta) else "서로 다른 방향으로 변한"
    insight = (
        f"{start}~{end} CSD 활동은 {_plain(activity_series[start])}건에서 {_plain(activity_series[end])}건, "
        f"{source} 매출은 {_eok(market_series[start])}억원에서 {_eok(market_series[end])}억원으로 {direction} 시점이 겹칩니다. "
        "이는 시점 대조이며 인과를 단정하지 않습니다."
    )
    chart = {
        "chart_type": "dual_axis_line",
        "title": f"CSD 활동과 {source} 매출 시점 대조",
        "source": f"CSD+{source} side-by-side",
        "scope": "MARKET",
        "evidence_refs": ["CSD.render_data.series", f"{source}.render_data.brand_value_series_10pt"],
        "labels": labels,
        "axes": {"y": {"unit": "건"}, "y1": {"unit": "KRW", "position": "right"}},
        "datasets": [
            {"label": "CSD 활동(건)", "unit": "건", "yAxisID": "y", "data": [_float(activity_series[p]) for p in labels]},
            {"label": f"{source} 매출(KRW)", "unit": "KRW", "yAxisID": "y1", "data": [_float(market_series[p]) for p in labels]},
        ],
    }
    return {
        "source": source, "period": f"{start}~{end}",
        "activity_change_rate_pct": _percent(changes.activity_change_rate),
        "performance_change_rate_pct": _percent(changes.performance_change_rate),
        "insight": insight, "chart": chart,
    }


def _forecast_call(calls: list[Call]) -> Call | None:
    results = [result for market in _market_calls(calls) if (result := _forecast_source_result(market))]
    if not results:
        return None
    primary = results[0]
    return _analysis_call(
        "A2", "conditional_trend_forecast", [str(result.pop("insight")) for result in results],
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=[result["source"] for result in results],
        never_aggregate_sources=True, forecast_is_trend_extension=True, forecast_uncertainty=True,
    )


def _forecast_source_result(market: Call) -> dict[str, Any] | None:
    series = _series(market, "brand_value_series_10pt", "value_krw")
    valid = [(period, value) for period, value in series.items() if value is not None]
    if len(valid) < 2:
        return None
    rate = calculate_cagr(valid[0][1], valid[-1][1], periods=Decimal(len(valid) - 1))
    forecast = conditional_trend_forecast(
        baseline=valid[-1][1], trend_rate=rate, threshold=Decimal("0"), periods=Decimal("1"),
    )
    if rate is None or forecast is None:
        return None
    source = _source_label(market)
    return {
        "source": source, "period": f"{valid[0][0]}~{valid[-1][0]}",
        "trend_rate_pct": _percent(rate), "forecast_krw": _float(forecast),
        "insight": (
        f"{source} {valid[0][0]}~{valid[-1][0]} 관측 성장률 {_pct(rate)}가 유지된다는 단순 추세 연장 시 "
        f"다음 기간 값은 {_eok(forecast)}억원입니다. 신규 진입·약가 변화 등 외부 요인은 반영하지 않은 조건부 계산입니다."
        ),
    }


def _patient_ratio_call(calls: list[Call]) -> Call | None:
    patient = _patient_count(calls)
    results = [
        result
        for market in _market_calls(calls)
        if patient is not None and (result := _patient_source_result(patient, market))
    ]
    if not results:
        return None
    primary = results[0]
    market_sources = [result["source"] for result in results]
    return _analysis_call(
        "A3", "patient_sales_ratio", [str(result.pop("insight")) for result in results],
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=["HIRA", *market_sources],
        evidence_refs=["HIRA.render_data.items.ptntCnt", *(f"{s}.render_data.brand_value_series_10pt" for s in market_sources)],
        never_aggregate_sources=True,
    )


def _patient_source_result(patient: tuple[str, Decimal], market: Call) -> dict[str, Any] | None:
    period, sales = _period_and_latest(market)
    ratio = patient_sales_ratio(sales=sales, patients=patient[1])
    if ratio is None:
        return None
    source = _source_label(market)
    return {
        "source": source, "period": period, "patient_period": patient[0],
        "patient_count": _float(patient[1]), "sales_krw": _float(sales),
        "sales_per_patient_krw": _float(ratio),
        "insight": (
        f"HIRA {patient[0]} 환자수 {_plain(patient[1])}명과 {source} {period} 브랜드 매출 {_eok(sales)}억원을 "
        f"나란히 보면 관측비는 환자 1명당 {_plain(ratio)}원입니다. 기간·정의가 다른 두 값을 합산한 수치는 아닙니다."
        ),
    }


def _seller_axis_call(calls: list[Call]) -> Call | None:
    if not any(call.get("tool") == "csd_activity_trend" for call in calls):
        return None
    insight = "현재 CSD 도구는 TOTAL 제품 활동 시계열만 제공해 판매사별 활동 변화는 현재 미지원입니다. 브랜드 활동으로 대체하지 않습니다."
    return _analysis_call("D3", "seller_activity_share_delta", [insight], status="unsupported_axis")


def _analysis_call(contract_id: str, calculation: str, insights: list[str], **data: Any) -> Call:
    return {
        "source": "BQ deterministic evidence",
        "tool": "bq_analysis",
        "summary_text": " ".join(insights),
        "render_data": {"contract_id": contract_id, "calculation": calculation, "insights": insights, **data},
    }


def _market_source(call: Call) -> str:
    data = call.get("render_data")
    spec = data.get("query_spec") if isinstance(data, dict) else None
    return str(spec.get("source") or "") if isinstance(spec, dict) else ""


def _market_calls(calls: list[Call]) -> list[Call]:
    return [call for call in calls if _market_source(call) in {"ubist", "iqvia_nsa"}]


def _source_label(call: Call) -> str:
    return "IQVIA NSA" if _market_source(call) == "iqvia_nsa" else "UBIST"


def _period_and_latest(call: Call) -> tuple[str, Decimal | None]:
    data = call.get("render_data")
    if not isinstance(data, dict):
        return "", None
    return str(data.get("period") or ""), _decimal(data.get("sales_krw"))


def _series(call: Call, key: str, value_key: str) -> dict[str, Decimal | None]:
    data = call.get("render_data")
    rows = data.get(key) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("period") or ""): _decimal(row.get(value_key))
        for row in rows if isinstance(row, dict) and row.get("period")
    }


def _patient_count(calls: list[Call]) -> tuple[str, Decimal] | None:
    for call in calls:
        data = call.get("render_data")
        nested = data.get("calls") if isinstance(data, dict) else None
        if not isinstance(nested, list):
            continue
        for child in nested:
            child_data = child.get("render_data") if isinstance(child, dict) else None
            items = child_data.get("items") if isinstance(child_data, dict) else None
            for item in items if isinstance(items, list) else []:
                value = _decimal(item.get("ptntCnt")) if isinstance(item, dict) else None
                if value is not None:
                    return str(item.get("year") or item.get("yyyy") or ""), value
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _percent(value: Decimal | None) -> float | None:
    return None if value is None else float(value * Decimal("100"))


def _eok(value: Decimal | None) -> str:
    return "—" if value is None else f"{value / Decimal('100000000'):,.2f}"


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * Decimal('100'):.2f}%"


def _plain(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _same_direction(left: Decimal | None, right: Decimal | None) -> bool:
    return left is not None and right is not None and left * right > 0


_BUILDERS: Final[dict[str, Builder]] = {
    "A2": _forecast_call,
    "A3": _patient_ratio_call,
    "C3": _source_divergence_call,
    "D2": _activity_alignment_call,
    "D3": _seller_axis_call,
}
