from __future__ import annotations

from typing import Any

from scripts.compose_ab_poc.mart_store import MartStore
from scripts.compose_ab_poc.models import AnalysisResult, StepTrace


def execute_intent(store: MartStore, intent_id: str) -> AnalysisResult:
    """Run deterministic calculations for a selected PoC intent."""

    handlers = {
        "brand_pair_sales_trend": _brand_pair_sales_trend,
        "top_share_trend": _top_share_trend,
        "share_decline_context": _share_decline_context,
        "market_vs_brand_feb": _market_vs_brand_feb,
        "competition_change": _competition_change,
        "atozet_threat": _atozet_threat,
        "atozet_livaro_cross_trend": _atozet_livaro_cross_trend,
        "news_sales_effect": _news_sales_effect,
        "livaro_yoy_growth": _livaro_yoy_growth,
        "livaro_avg_share_6m": _livaro_avg_share_6m,
        "market_concentration": _market_concentration,
        "top5_share_sum": _top5_share_sum,
        "target_share_gap": _target_share_gap,
        "clinic_channel_molecule_share": _clinic_channel_molecule_share,
        "livaro_atozet_channel_diff": _livaro_atozet_channel_diff,
        "ox_gx_mix": _ox_gx_mix,
        "top_competitor_specialty_sales": _top_competitor_specialty_sales,
        "class_sales_trend_12m": _class_sales_trend_12m,
        "top_company_molecule": _top_company_molecule,
        "nhi_mix_trend": _nhi_mix_trend,
    }
    handler = handlers.get(intent_id)
    if handler is None:
        return AnalysisResult(intent_id, "error", {"error": "unknown_intent"}, "지원하지 않는 intent입니다.", ("error",))
    return handler(store)


def primitive_trace(intent_id: str, result: AnalysisResult) -> list[StepTrace]:
    """Represent the deterministic execution as primitive-chain tool calls."""

    rows = len(result.facts.get("rows", ())) if isinstance(result.facts.get("rows"), list) else None
    trace = [
        StepTrace("fetch", {"source": "ubist", "view": "market_landscape", "market": "ml_006"}, "r_fetch", rows=470),
        StepTrace("filter", {"result_id": "r_fetch", "conditions": _intent_filters(intent_id)}, "r_filter", rows=rows),
        StepTrace("group_by", {"result_id": "r_filter", "keys": _intent_group_keys(intent_id)}, "r_group", rows=rows),
        StepTrace("aggregate", {"result_id": "r_group", "metric": "sales", "func": _intent_aggregate(intent_id)}, "r_agg", rows=rows),
        StepTrace(_intent_compute_tool(intent_id), {"intent_id": intent_id, "formulas": result.fact_keys}, "r_final", summary=result.status),
    ]
    return trace


def query_spec_trace(intent_id: str, result: AnalysisResult) -> list[StepTrace]:
    """Represent the deterministic execution as query(spec) plus compute tool calls."""

    spec = {
        "source": "ubist",
        "view": "market_landscape",
        "market": "ml_006",
        "filters": _intent_filters(intent_id),
        "group_by": _intent_group_keys(intent_id),
        "metrics": ["sales", "share", "rank"],
        "derive": list(result.fact_keys),
        "sort": "value_desc",
        "limit": _intent_limit(intent_id),
    }
    return [
        StepTrace("query", {"spec": spec}, "q_result", summary=result.status),
        StepTrace(_intent_compute_tool(intent_id), {"result_id": "q_result", "formulas": result.fact_keys}, "q_final", summary=result.status),
    ]


def _brand_pair_sales_trend(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(6)
    rows = _series_rows(store, ("리바로", "리바로젯"), periods)
    return _ok("brand_pair_sales_trend", {"periods": periods, "rows": rows}, "최근 6개월 리바로/리바로젯 매출 추이를 계산했습니다.")


def _top_share_trend(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(6)
    brands = tuple(row["brand"] for row in store.top_brands(3))
    rows = _series_rows(store, brands, periods)
    return _ok("top_share_trend", {"brands": brands, "periods": periods, "rows": rows}, "상위 3개 브랜드의 최근 6개월 점유율 변화를 계산했습니다.")


def _share_decline_context(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(6)
    livaro = store.brand_series("리바로", periods)
    top5 = store.top_brands(5)
    delta = _delta(livaro[0]["share_pct"], livaro[-1]["share_pct"])
    facts = {"periods": periods, "livaro_series": livaro, "share_delta_p": delta, "top5": top5}
    return _ok("share_decline_context", facts, "리바로 점유율 추이와 상위 브랜드 구도를 제시하되 인과는 단정하지 않았습니다.")


def _market_vs_brand_feb(store: MartStore) -> AnalysisResult:
    periods = ("2026-01", "2026-02", "2026-03")
    brand = store.brand_series("리바로", periods)
    market = store.market_series(periods)
    facts = {
        "periods": periods,
        "brand": brand,
        "market": market,
        "brand_jan_feb_pct": _growth(brand[0]["value"], brand[1]["value"]),
        "market_jan_feb_pct": _growth(market[0]["value"], market[1]["value"]),
        "brand_feb_mar_pct": _growth(brand[1]["value"], brand[2]["value"]),
        "market_feb_mar_pct": _growth(market[1]["value"], market[2]["value"]),
    }
    return _ok("market_vs_brand_feb", facts, "2월 하락이 시장과 같은 방향인지 브랜드 고유 움직임인지 대조했습니다.")


def _competition_change(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(6)
    brands = tuple(row["brand"] for row in store.top_brands(5))
    rows = _series_rows(store, brands, periods)
    return _ok("competition_change", {"brands": brands, "periods": periods, "rows": rows}, "상위 5개 브랜드의 최근 점유율·순위 변화로 경쟁 구도를 계산했습니다.")


def _atozet_threat(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(6)
    rows = _series_rows(store, ("리바로", "아토젯"), periods)
    facts = {"periods": periods, "rows": rows, "latest": {brand: rows[brand][-1] for brand in rows}}
    return _ok("atozet_threat", facts, "아토젯과 리바로의 최신 순위·점유율과 6개월 흐름을 비교했습니다.")


def _atozet_livaro_cross_trend(store: MartStore) -> AnalysisResult:
    result = _atozet_threat(store)
    facts = dict(result.facts)
    facts["atozet_share_delta_p"] = _delta(facts["rows"]["아토젯"][0]["share_pct"], facts["rows"]["아토젯"][-1]["share_pct"])
    facts["livaro_share_delta_p"] = _delta(facts["rows"]["리바로"][0]["share_pct"], facts["rows"]["리바로"][-1]["share_pct"])
    return _ok("atozet_livaro_cross_trend", facts, "아토젯 상승/하락 구간과 리바로 점유율 동행을 비교했습니다.")


def _news_sales_effect(_store: MartStore) -> AnalysisResult:
    facts = {"unsupported_reason": "공통 PoC 입력은 mart_strategic_ml_brand_metric만이며 뉴스 이벤트/효과 귀속 데이터가 없습니다."}
    return _unsupported("news_sales_effect", facts)


def _livaro_yoy_growth(store: MartStore) -> AnalysisResult:
    recent = store.recent_period
    previous = "2025-04"
    facts = {
        "brand": "리바로",
        "recent_period": recent,
        "previous_period": previous,
        "recent_value": store.value("리바로", recent),
        "previous_value": store.value("리바로", previous),
        "yoy_growth_pct": _growth(store.value("리바로", previous), store.value("리바로", recent)),
    }
    return _ok("livaro_yoy_growth", facts, "최신월과 전년 동월 매출로 YoY 성장률을 계산했습니다.")


def _livaro_avg_share_6m(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(6)
    rows = store.brand_series("리바로", periods)
    avg = sum(row["share_pct"] for row in rows) / len(rows)
    return _ok("livaro_avg_share_6m", {"periods": periods, "rows": rows, "avg_share_pct": avg}, "최근 6개월 리바로 평균 점유율을 계산했습니다.")


def _market_concentration(store: MartStore) -> AnalysisResult:
    top3 = store.top_brands(3)
    top5 = store.top_brands(5)
    facts = {
        "period": store.recent_period,
        "brand_count": len(store.records),
        "hhi": store.hhi(),
        "top3_share_pct": sum(row["share_pct"] for row in top3),
        "top5_share_pct": sum(row["share_pct"] for row in top5),
        "top5": top5,
    }
    return _ok("market_concentration", facts, "HHI와 상위 브랜드 비중으로 시장 집중도를 계산했습니다.")


def _top5_share_sum(store: MartStore) -> AnalysisResult:
    top5 = store.top_brands(5)
    return _ok("top5_share_sum", {"period": store.recent_period, "top5": top5, "top5_share_pct": sum(row["share_pct"] for row in top5)}, "상위 5개 브랜드 점유율 합계를 계산했습니다.")


def _target_share_gap(store: MartStore) -> AnalysisResult:
    market = store.market_value(store.recent_period)
    current = store.value("리바로", store.recent_period)
    target = market * 0.04
    facts = {"period": store.recent_period, "market_value": market, "current_value": current, "target_value": target, "needed_increase": target - current}
    return _ok("target_share_gap", facts, "현재 시장규모 기준 4% 점유율 회복에 필요한 매출 증가분을 계산했습니다.")


def _clinic_channel_molecule_share(store: MartStore) -> AnalysisResult:
    rows = store.channel_molecule_share("의원")[:10]
    return _ok("clinic_channel_molecule_share", {"period": store.recent_period, "channel": "의원", "rows": rows}, "의원 채널 매출을 성분별로 그룹화해 점유율을 계산했습니다.")


def _livaro_atozet_channel_diff(store: MartStore) -> AnalysisResult:
    rows = store.channel_brand_shares(("리바로", "아토젯"))
    for row in rows:
        row["share_diff_p"] = row["아토젯_share_pct"] - row["리바로_share_pct"]
    return _ok("livaro_atozet_channel_diff", {"period": store.recent_period, "rows": rows}, "채널별 시장 내 리바로/아토젯 점유율 차이를 계산했습니다.")


def _ox_gx_mix(store: MartStore) -> AnalysisResult:
    return _ok("ox_gx_mix", {"period": store.recent_period, "rows": store.ox_gx_mix()}, "최신월 오리지널/Ox와 제네릭/Gx 구성비를 계산했습니다.")


def _top_competitor_specialty_sales(store: MartStore) -> AnalysisResult:
    brands = tuple(row["brand"] for row in store.top_brands(3, exclude={"리바로"}))
    return _ok("top_competitor_specialty_sales", {"period": store.recent_period, "brands": brands, "specialties": store.specialty_sales(brands)}, "상위 경쟁 브랜드 3개의 진료과별 매출을 계산했습니다.")


def _class_sales_trend_12m(store: MartStore) -> AnalysisResult:
    periods = store.last_periods(12)
    rows = store.group_series("class_label", periods)
    return _ok("class_sales_trend_12m", {"periods": periods, "rows": rows, "note": "mart by_dimension의 class를 제형 proxy로 사용했습니다."}, "최근 12개월 class별 매출 추이를 계산했습니다.")


def _top_company_molecule(store: MartStore) -> AnalysisResult:
    rows = store.top_company_molecules(3)
    return _ok("top_company_molecule", {"period": store.recent_period, "rows": rows}, "회사 top3와 각 회사의 주요 성분 구성을 계산했습니다.")


def _nhi_mix_trend(_store: MartStore) -> AnalysisResult:
    facts = {"unsupported_reason": "ml_006 mart row의 by_dimension에는 급여/비급여(nhi_type) 차원이 없어 이 입력만으로 구성/추이를 계산할 수 없습니다."}
    return _unsupported("nhi_mix_trend", facts)


def _series_rows(store: MartStore, brands: tuple[str, ...], periods: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    return {brand: store.brand_series(brand, periods) for brand in brands}


def _ok(intent_id: str, facts: dict[str, Any], message: str) -> AnalysisResult:
    return AnalysisResult(intent_id, "ok", facts, _answer_md(intent_id, facts, message), tuple(sorted(facts)), (message,))


def _unsupported(intent_id: str, facts: dict[str, Any]) -> AnalysisResult:
    note = str(facts["unsupported_reason"])
    return AnalysisResult(intent_id, "unsupported", facts, f"{note}\n\n출처: UBIST mart snapshot", tuple(sorted(facts)), (note,))


def _answer_md(intent_id: str, facts: dict[str, Any], message: str) -> str:
    rows = _preview_rows(facts)
    return f"### {intent_id}\n\n{message}\n\n```json\n{rows}\n```\n\n출처: UBIST mart_strategic_ml_brand_metric"


def _preview_rows(facts: dict[str, Any]) -> str:
    import json

    return json.dumps(_compact(facts), ensure_ascii=False, indent=2)


def _compact(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return "..."
    if isinstance(value, dict):
        return {key: _compact(item, depth + 1) for key, item in list(value.items())[:8]}
    if isinstance(value, list):
        return [_compact(item, depth + 1) for item in value[:6]]
    if isinstance(value, tuple):
        return [_compact(item, depth + 1) for item in value[:6]]
    if isinstance(value, float):
        return round(value, 4)
    return value


def _growth(start: float, end: float) -> float | None:
    return (end / start - 1.0) * 100.0 if start else None


def _delta(start: float, end: float) -> float:
    return end - start


def _intent_filters(intent_id: str) -> dict[str, Any]:
    return {
        "brand_pair_sales_trend": {"brand": ["리바로", "리바로젯"], "period": "last6"},
        "top_share_trend": {"top_n": 3, "period": "last6"},
        "clinic_channel_molecule_share": {"channel": "의원", "period": "latest"},
        "livaro_atozet_channel_diff": {"brand": ["리바로", "아토젯"], "period": "latest"},
    }.get(intent_id, {"period": "latest_or_question_window"})


def _intent_group_keys(intent_id: str) -> list[str]:
    if intent_id == "clinic_channel_molecule_share":
        return ["molecule"]
    if intent_id == "livaro_atozet_channel_diff":
        return ["channel", "product"]
    if intent_id == "top_company_molecule":
        return ["company", "molecule"]
    if intent_id == "top_competitor_specialty_sales":
        return ["product", "specialty"]
    if intent_id == "ox_gx_mix":
        return ["ox_gx"]
    if intent_id == "class_sales_trend_12m":
        return ["dosage_form", "period"]
    return ["product", "period"]


def _intent_aggregate(intent_id: str) -> str:
    return "series_sum" if "trend" in intent_id or "series" in intent_id else "sum"


def _intent_limit(intent_id: str) -> int | None:
    if intent_id in {"top_share_trend", "top_company_molecule"}:
        return 3
    if intent_id in {"competition_change", "top5_share_sum"}:
        return 5
    return None


def _intent_compute_tool(intent_id: str) -> str:
    if intent_id in {"market_concentration", "top5_share_sum"}:
        return "compute_hhi"
    if intent_id in {"livaro_yoy_growth"}:
        return "compute_growth"
    if intent_id in {"target_share_gap"}:
        return "compute_gap"
    if intent_id in {"ox_gx_mix", "nhi_mix_trend"}:
        return "compute_mix"
    if "delta" in intent_id or "diff" in intent_id or "threat" in intent_id:
        return "compute_delta"
    if "share" in intent_id:
        return "compute_share"
    return "compute_series"
