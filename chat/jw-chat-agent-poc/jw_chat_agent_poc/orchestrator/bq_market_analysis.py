from __future__ import annotations

from decimal import Decimal
from statistics import fmean, pstdev
from typing import Any, Callable, Final

from jw_chat_agent_poc.orchestrator.bq_calculations import (
    calculate_cagr,
    cohort_z_score,
    market_vs_brand_growth_decomposition,
    share_of_growth,
)
from jw_chat_agent_poc.orchestrator.bq_causal_analysis import build_causal_analysis_call
from jw_chat_agent_poc.orchestrator.bq_context_analysis import build_context_analysis_call
from jw_chat_agent_poc.orchestrator.bq_market_evidence import (
    aligned_endpoints as _aligned_endpoints,
    analysis as _analysis,
    brand_rows as _brand_rows,
    data as _data,
    decimal_text as _plain,
    decimal_value as _decimal,
    distribution as _distribution,
    percentage_point_text as _pctp,
    percentage_text as _pct,
    percent as _percent,
    rate as _rate,
    segments as _segments,
    source_key as _source_key,
    source_label as _source_label,
    source_line_charts as _source_line_charts,
    source_series_calls as _source_series_calls,
    to_float as _float,
    top_call as _top_call,
    top_calls as _top_calls,
    top_trend as _top_trend,
    top_trend_from_call as _top_trend_from_call,
    year_span as _year_span,
)

Call = dict[str, Any]
Builder = Callable[[list[Call]], Call | None]


def build_market_analysis_call(contract_id: str, calls: list[Call]) -> Call | None:
    builder = _BUILDERS.get(contract_id)
    if builder is not None:
        return builder(calls)
    if contract_id == "E2":
        return build_causal_analysis_call(calls)
    return build_context_analysis_call(contract_id, calls)


def _market_overview(calls: list[Call]) -> Call | None:
    summaries = [_source_summary(call) for call in _source_series_calls(calls)]
    source_summaries = [item for item in summaries if item is not None]
    if not source_summaries:
        return None
    channels = _distribution(calls, "channel")
    insights = [_growth_insight(item) for item in source_summaries]
    return _analysis(
        "A1", "market_growth_and_channel_share", insights,
        source_summaries=source_summaries, channel_shares_pct=channels,
        never_aggregate_sources=True, source_labels=[item["source"] for item in source_summaries],
        chart_payloads=_source_line_charts(calls),
    )


def _competition_change(calls: list[Call]) -> Call | None:
    results = [result for call in _source_series_calls(calls) if (result := _competition_result(call, calls))]
    if not results:
        return None
    primary = results[0]
    charts = [result.pop("chart") for result in results]
    insights = [str(result.pop("insight")) for result in results]
    return _analysis(
        "B1", "competition_change", insights,
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=[result["source"] for result in results],
        never_aggregate_sources=True, chart_payloads=charts,
    )


def _competition_result(series_call: Call, calls: list[Call]) -> dict[str, Any] | None:
    endpoints = _aligned_endpoints(series_call)
    if endpoints is None:
        return None
    trend_rows = _top_trend(calls, _source_key(series_call))
    start_period, end_period, brand_start, brand_end, market_start, market_end, share_start, share_end = endpoints
    brand_delta = brand_end - brand_start
    market_delta = market_end - market_start
    brand_rate = _rate(brand_start, brand_end)
    market_rate = _rate(market_start, market_end)
    decomposition = market_vs_brand_growth_decomposition(brand_growth=brand_rate, market_growth=market_rate)
    contribution = share_of_growth(brand_growth=brand_delta, market_growth=market_delta)
    gain_loss = [
        {
            "brand": str(row.get("brand") or ""),
            "share_delta_pctp": _float(_decimal(row.get("share_delta_pctp"))),
            "sales_delta_krw": _float(_decimal(row.get("value_delta_krw"))),
        }
        for row in trend_rows if row.get("brand")
    ]
    source = _source_label(series_call)
    return {
        "source": source, "period": f"{start_period}~{end_period}",
        "brand_growth_pct": _percent(brand_rate), "market_growth_pct": _percent(market_rate),
        "excess_growth_pctp": _percent(decomposition.excess_growth),
        "share_of_growth_pct": _percent(contribution), "share_delta_pctp": _float(share_end - share_start),
        "gain_loss": gain_loss,
        "insight": (
            f"{source} {start_period}~{end_period} 브랜드 성장률 {_pct(brand_rate)}와 "
            f"시장 성장률 {_pct(market_rate)}의 차이는 {_pctp(decomposition.excess_growth)}이며, "
            f"시장 성장분 중 브랜드 몫(share-of-growth)은 {_pct(contribution)}입니다."
        ),
        "chart": {
            "chart_type": "waterfall", "title": f"{source} 브랜드별 점유율 변화", "source": source,
            "scope": "MARKET", "unit": "%p", "evidence_refs": [f"{source}.level_top5_trend_series"],
            "labels": [row["brand"] for row in gain_loss],
            "datasets": [{"label": "점유율 변화(%p)", "unit": "%p", "data": [row["share_delta_pctp"] for row in gain_loss]}],
        },
    }


def _cohort_position(calls: list[Call]) -> Call | None:
    results = [result for series in _source_series_calls(calls) if (result := _cohort_result(series, calls))]
    if not results:
        return None
    primary = results[0]
    insights = [str(result.pop("insight")) for result in results]
    return _analysis(
        "B2", "cohort_z_score", insights,
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=[result["source"] for result in results],
        never_aggregate_sources=True,
    )


def _cohort_result(series: Call, calls: list[Call]) -> dict[str, Any] | None:
    top = _top_call(calls, _source_key(series))
    if top is None:
        return None
    rows = _segments(top)
    values = [_decimal(row.get("value")) for row in rows]
    population_values = [value for value in values if value is not None]
    brand = str(_data(series).get("brand") or "")
    anchor = next((_decimal(row.get("value")) for row in rows if str(row.get("name") or "") == brand), None)
    if anchor is None or len(population_values) < 2:
        return None
    floats = [float(value) for value in population_values]
    mean = Decimal(str(fmean(floats)))
    deviation = Decimal(str(pstdev(floats)))
    score = cohort_z_score(value=anchor, mean=mean, stddev=deviation)
    source = _source_label(series)
    return {
        "source": source, "cohort_z_score": _float(score),
        "population": len(population_values), "competition_basis": "same market source and period",
        "insight": (
            f"{source} {brand}의 동일 시장·출처·기간 cohort 매출 z-score는 {_plain(score)}이며 "
            f"모집단은 {len(population_values)}개 브랜드입니다."
        ),
    }


def _threat_growth(calls: list[Call]) -> Call | None:
    results = [result for call in _top_calls(calls) if (result := _growth_rank_result(call))]
    if not results:
        return None
    primary = results[0]
    return _analysis(
        "B3", "growth_rank", [str(result.pop("insight")) for result in results],
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=[result["source"] for result in results],
        never_aggregate_sources=True,
    )


def _growth_rank_result(call: Call) -> dict[str, Any] | None:
    rows = _top_trend_from_call(call)
    if not rows:
        return None
    ranking = sorted(
        ({"brand": str(row.get("brand") or ""), "sales_delta_krw": _float(_decimal(row.get("value_delta_krw"))), "share_delta_pctp": _float(_decimal(row.get("share_delta_pctp")))} for row in rows),
        key=lambda row: row["sales_delta_krw"] if row["sales_delta_krw"] is not None else float("-inf"), reverse=True,
    )
    source = _source_label(call)
    return {
        "source": source, "growth_ranking": ranking,
        "launch_acceleration_status": "unsupported_missing_launch_date",
        "insight": (
            f"{source} 출시일 근거가 없어 출시 후 가속도는 계산하지 않으며, "
            "관측 구간 성장 순위만 제시합니다."
        ),
    }


def _brand_market_gap(calls: list[Call]) -> Call | None:
    results = [result for call in _source_series_calls(calls) if (result := _brand_gap_result(call))]
    if not results:
        return None
    primary = results[0]
    return _analysis(
        "C1", "brand_market_growth_gap", [str(result.pop("insight")) for result in results],
        **{key: value for key, value in primary.items() if key != "source"},
        source_results=results, source_labels=[result["source"] for result in results],
        never_aggregate_sources=True,
    )


def _brand_gap_result(series_call: Call) -> dict[str, Any] | None:
    endpoints = _aligned_endpoints(series_call)
    if endpoints is None:
        return None
    start_period, end_period, brand_start, brand_end, market_start, market_end, _, _ = endpoints
    brand_rate = _rate(brand_start, brand_end)
    market_rate = _rate(market_start, market_end)
    gap = None if brand_rate is None or market_rate is None else brand_rate - market_rate
    count = max(1, len(_brand_rows(series_call)) - 1)
    slope = (brand_end - brand_start) / Decimal(count)
    source = _source_label(series_call)
    return {
        "source": source, "period": f"{start_period}~{end_period}",
        "brand_growth_pct": _percent(brand_rate),
        "market_growth_pct": _percent(market_rate),
        "growth_gap_pctp": _percent(gap), "trend_slope_krw_per_period": _float(slope),
        "insight": (
            f"{source} {start_period}~{end_period} 브랜드 성장률 {_pct(brand_rate)}, "
            f"시장 성장률 {_pct(market_rate)}, 성장률 차이 {_pctp(gap)}입니다."
        ),
    }


def _source_summary(call: Call) -> dict[str, Any] | None:
    endpoints = _aligned_endpoints(call)
    if endpoints is None:
        return None
    start_period, end_period, brand_start, brand_end, _, _, _, _ = endpoints
    years = max(Decimal("1"), _year_span(start_period, end_period))
    growth = calculate_cagr(brand_start, brand_end, periods=years)
    return {
        "source": _source_label(call), "start_period": start_period, "end_period": end_period,
        "start_sales_krw": _float(brand_start), "end_sales_krw": _float(brand_end),
        "growth_rate_pct": _percent(growth), "growth_basis_years": _float(years),
    }


def _growth_insight(item: dict[str, Any]) -> str:
    prefix = f"{item['source']} {item['start_period']}~{item['end_period']} 브랜드 매출"
    growth = item.get("growth_rate_pct")
    if growth is None:
        return f"{prefix}은 기준값이 0이어서 성장률을 계산할 수 없습니다."
    return f"{prefix} 성장률은 {float(growth):.2f}%입니다."


_BUILDERS: Final[dict[str, Builder]] = {
    "A1": _market_overview, "B1": _competition_change, "B2": _cohort_position,
    "B3": _threat_growth, "C1": _brand_market_gap,
}
