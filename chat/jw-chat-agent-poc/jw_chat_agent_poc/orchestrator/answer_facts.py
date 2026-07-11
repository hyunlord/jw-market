from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    TABLE_LIMIT,
    cell,
    eok_value,
    items,
    number_value,
    pct_value,
    rank_value,
    source_label,
    table,
)
from jw_chat_agent_poc.orchestrator.dosage_notes import dosage_combination_note
from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_fact_markdown
from jw_chat_agent_poc.orchestrator.surface_policy import (
    DeltaOperands,
    can_surface_derived_value,
    cagr_operands_from_data,
    request_value,
    surface_year,
)
from jw_chat_agent_poc.tools.query_layer.spec import display_level_name

RenderData = dict[str, Any]
RequiredFactCollector = Callable[[RenderData, str], tuple["AxisFact", ...]]
HiraRow = tuple[Any, Any, Any, Any, str]
TOP_TREND_DELTA_WITHHELD: Final[str] = "점유율 변화 표시 보류(시작/최신 MS 또는 반올림 정합 미확인)"
CONFIRMED_MARKET_VIEW_BY_ID: Final[dict[str, str]] = {
    **{f"strategy_{idx:03d}": "market_landscape" for idx in range(1, 17)},
    **{f"ml_{idx:03d}": "market_landscape" for idx in range(1, 17)},
    **{f"cd_{idx:03d}": "competitive_dynamics" for idx in range(1, 20)},
}
CONFIRMED_MARKET_LANDSCAPE_COUNTERPART_BY_ID: Final[dict[str, str]] = {
    **{f"strategy_{idx:03d}": f"ml_{idx:03d}" for idx in range(1, 17)},
    **{f"ml_{idx:03d}": f"strategy_{idx:03d}" for idx in range(1, 17)},
}
CONFIRMED_MARKET_LANDSCAPE_COUNTERPART_DENOMINATOR_BY_ID: Final[dict[str, int]] = {
    "strategy_006": 470,
    "ml_006": 516,
}
VIEW_NAME_BY_INTERNAL_LABEL: Final[dict[str, str]] = {
    "market_landscape": "market_landscape",
    "strategic_ml": "market_landscape",
    "competitive_dynamics": "competitive_dynamics",
    "strategic_cd": "competitive_dynamics",
    "general": "general",
    "general_view": "general",
}


class RequiredAxis(StrEnum):
    """Question axes that independently contribute mandatory answer facts."""

    SALES_TREND = "sales_trend"
    MARKET_STRUCTURE = "market_structure"
    PATIENT_VOLUME = "patient_volume"
    ISSUE_CONTEXT = "issue_context"
    BRAND_POSITION = "brand_position"
    PATENT_EXCLUSIVITY = "patent_exclusivity"
    CSD_ACTIVITY = "csd_activity"


class MetricFactDetail(StrEnum):
    """How much context a metric call should render outside mandatory axis facts."""

    FULL = "full"
    CORE_ONLY = "core_only"
    MONTHLY_ONLY = "monthly_only"


@dataclass(frozen=True, slots=True)
class AxisFact:
    """One mandatory fact row owned by a single answer axis."""

    axis: RequiredAxis
    label: str
    content: str


@dataclass(frozen=True, slots=True)
class ValueProvenanceFact:
    """One surfaced numeric value and the query metadata that produced it."""

    value_label: str
    source: str
    period: str
    market: str
    axis: str
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class TopTrendShareDelta:
    from_period: str
    from_ms_pct: Any
    to_period: str
    to_ms_pct: Any
    delta_pctp: Any


_REQUIRED_AXIS_ORDER: Final[tuple[RequiredAxis, ...]] = (
    RequiredAxis.SALES_TREND,
    RequiredAxis.MARKET_STRUCTURE,
    RequiredAxis.PATIENT_VOLUME,
    RequiredAxis.ISSUE_CONTEXT,
    RequiredAxis.BRAND_POSITION,
    RequiredAxis.PATENT_EXCLUSIVITY,
    RequiredAxis.CSD_ACTIVITY,
)

_REQUIRED_METRIC_AXES: Final[dict[str, tuple[RequiredAxis, ...]]] = {
    "sales_delta": (RequiredAxis.SALES_TREND,),
    "market_share_delta": (RequiredAxis.MARKET_STRUCTURE,),
    "market_vs_brand_delta": (RequiredAxis.SALES_TREND, RequiredAxis.MARKET_STRUCTURE),
    "brand_trend_comparison": (RequiredAxis.MARKET_STRUCTURE,),
    "competitive_insight_signals": (RequiredAxis.MARKET_STRUCTURE,),
    "market_member_snapshot": (RequiredAxis.MARKET_STRUCTURE,),
    "yoy_growth": (RequiredAxis.SALES_TREND,),
    "average_share": (RequiredAxis.MARKET_STRUCTURE,),
    "sales": (RequiredAxis.SALES_TREND, RequiredAxis.BRAND_POSITION),
    "market_share": (RequiredAxis.MARKET_STRUCTURE, RequiredAxis.BRAND_POSITION),
    "share": (RequiredAxis.MARKET_STRUCTURE, RequiredAxis.BRAND_POSITION),
    "rank": (RequiredAxis.BRAND_POSITION,),
    "hira_disease": (RequiredAxis.PATIENT_VOLUME,),
    "hira_procedure": (RequiredAxis.PATIENT_VOLUME,),
}


def answer_fact_markdown(calls: list[dict[str, Any]], sources: list[str]) -> str:
    blocks: list[str] = ["## 확정 fact set"]
    seen_blocks: set[str] = set()
    required = _required_fact_block(calls)
    if required:
        blocks.append(required)
        seen_blocks.add(required)
    for call in calls:
        if _is_fact_only_completion_call(call):
            continue
        block = _call_fact_block(call, detail=_metric_fact_detail(call, calls))
        if block and block not in seen_blocks:
            blocks.append(block)
            seen_blocks.add(block)
    if len(blocks) == 1:
        blocks.append("- 표시할 확정 fact가 없습니다.")
    source_block = _source_block(calls, sources)
    if source_block:
        blocks.append(source_block)
    return "\n\n".join(blocks)


def _is_fact_only_completion_call(call: dict[str, Any]) -> bool:
    data = call.get("render_data")
    if not isinstance(data, dict) or not data.get("completion_reason"):
        return False
    return data.get("completion_reason") in {
        "comparison_trend_requires_series",
        "share_delta_requires_period_metrics",
        "largest_competitor_requires_member_metric",
    }


def _required_fact_block(calls: list[dict[str, Any]]) -> str:
    facts: list[AxisFact] = []
    for call in calls:
        facts.extend(_axis_facts_for_call(call))
    rows = _ordered_axis_rows(facts)
    if not rows:
        return ""
    return table("### 필수 답변 fact", ("구분", "반드시 반영할 내용"), tuple(rows))


def _axis_facts_for_call(call: dict[str, Any]) -> tuple[AxisFact, ...]:
    data = call.get("render_data")
    if not isinstance(data, dict):
        return ()
    contract_intent = str(data.get("contract_intent") or "")
    if contract_intent == "segment_compare":
        return _segment_compare_axis_facts(data)
    if contract_intent == "source_crosscheck":
        return _source_crosscheck_axis_facts(data)
    if contract_intent == "quarter_metric":
        return _quarter_metric_axis_facts(data)
    if data.get("status") in {"error", "query_failed"}:
        facts = [
            AxisFact(
                RequiredAxis.BRAND_POSITION,
                "조회 실패",
                str(data.get("message") or "요청 지표 조회 실행이 실패했습니다. 데이터 미보유로 해석하지 않습니다."),
            )
        ]
        structure_detail = _market_structure_detail([call])
        if structure_detail:
            facts.append(AxisFact(RequiredAxis.MARKET_STRUCTURE, "Class 구조 기준", structure_detail))
        return tuple(facts)
    if data.get("status") == "unsupported":
        return (
            AxisFact(
                RequiredAxis.BRAND_POSITION,
                "데이터 미보유",
                str(data.get("message") or "요청 지표를 현재 지원 데이터에서 확정하지 못했습니다."),
            ),
        )
    tool = str(call.get("tool") or "")
    if tool == "agent_calculation":
        return _agent_calculation_axis_facts(data)
    if tool == "portfolio_decline_analysis":
        return _portfolio_decline_axis_facts(data)
    if tool == "get_brand_metric":
        return _brand_metric_axis_facts(data)
    if tool == "csd_activity_trend":
        return _csd_activity_axis_facts(data)
    if "patent" in tool or "orangebook" in tool:
        return _patent_axis_facts(data)
    if _is_hira_disease_call(call):
        return tuple(AxisFact(RequiredAxis.PATIENT_VOLUME, label, content) for label, content in _required_hira_rows(data))
    if _is_hira_procedure_call(call):
        return tuple(AxisFact(RequiredAxis.PATIENT_VOLUME, label, content) for label, content in _required_hira_procedure_rows(data))
    return ()


def _csd_activity_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    status = str(data.get("status") or "")
    unsupported = ", ".join(str(item) for item in data.get("unsupported_fields", ()) if item)
    if status in {"unsupported", "no_data", "error", "query_failed"}:
        message = str(data.get("message") or "CSD ChannelDynamics aggregate 콜수/활동량 조회 결과를 확인하지 못했습니다.")
        return (
            AxisFact(RequiredAxis.CSD_ACTIVITY, "CSD aggregate 콜수", message),
            AxisFact(RequiredAxis.CSD_ACTIVITY, "CSD 세부 미지원", unsupported or "impact level, HCP/의사별, 기관별"),
        )
    series = data.get("series")
    if not isinstance(series, list) or not series:
        return (
            AxisFact(RequiredAxis.CSD_ACTIVITY, "CSD aggregate 콜수", "CSD ChannelDynamics aggregate 콜수/활동량 시계열 행이 없습니다."),
            AxisFact(RequiredAxis.CSD_ACTIVITY, "CSD 세부 미지원", unsupported or "impact level, HCP/의사별, 기관별"),
        )
    first = next((item for item in series if isinstance(item, dict)), {})
    latest = next((item for item in reversed(series) if isinstance(item, dict)), {})
    brand = str(data.get("brand") or "")
    source = str(data.get("source_label") or "CSD ChannelDynamics")
    first_text = _csd_activity_point(first)
    latest_text = _csd_activity_point(latest)
    return (
        AxisFact(
            RequiredAxis.CSD_ACTIVITY,
            "CSD aggregate 콜수",
            f"{brand} {source} aggregate 콜수/활동량 {first_text} → {latest_text}",
        ),
        AxisFact(
            RequiredAxis.CSD_ACTIVITY,
            "CSD 세부 미지원",
            unsupported or "impact level, HCP/의사별, 기관별",
        ),
    )


def _csd_activity_point(item: dict[str, Any]) -> str:
    period = str(item.get("period") or "")
    value = item.get("product_details")
    value_text = f"{int(value):,}건" if isinstance(value, (int, float)) else str(value or "")
    return " ".join(part for part in (period, value_text) if part)


def _segment_compare_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    axis = str(data.get("requested_axis") or data.get("level") or "요청 축")
    status = str(data.get("status") or "")
    if status in {"error", "query_failed", "unsupported", "mapping_failed", "missing", "incomplete_split"}:
        return (
            AxisFact(
                RequiredAxis.MARKET_STRUCTURE,
                f"{axis} 미지원",
                f"{axis} 축은 현재 catalog/query 경로에서 조회 성공하지 못했습니다. 값을 추정하지 않습니다.",
            ),
        )
    segments = _segment_compare_rows(data)
    if not segments:
        return (
            AxisFact(
                RequiredAxis.MARKET_STRUCTURE,
                f"{axis} 미지원",
                f"{axis} 축은 조회 결과 행이 없어 값을 표시하지 않습니다.",
            ),
        )
    top = next((item for item in segments if isinstance(item, dict)), {})
    if not top:
        return ()
    source = str(data.get("source_label") or data.get("source") or "보유 소스")
    period = str(data.get("period") or "최신 기간")
    name = _segment_display_name(top.get("name") or top.get("brand") or "상위 세그먼트")
    sales = eok_value(top.get("value_억원") or top.get("value_recent_억원"), top.get("value") or top.get("value_recent"))
    share = pct_value(top.get("ms_recent_pct") or top.get("to_ms_pct"))
    parts = [
        f"{source} {period} 기준 1위 {name}",
        f"매출 {sales}" if sales else "",
        f"MS {share}" if share else "",
    ]
    content = " ".join(part for part in parts if part)
    note = dosage_combination_note(axis, (item.get("name") or item.get("brand") for item in segments if isinstance(item, dict)))
    if note:
        content = f"{content} {note}"
    return (AxisFact(RequiredAxis.MARKET_STRUCTURE, f"{axis} 지원", content),)


def _segment_compare_rows(data: RenderData) -> list[dict[str, Any]]:
    segments = data.get("level_segments")
    if isinstance(segments, list) and segments:
        return [item for item in segments if isinstance(item, dict)]
    trends = data.get("level_top5_trend_series")
    if isinstance(trends, list) and trends:
        return [item for item in trends if isinstance(item, dict)]
    return []


def _segment_display_name(value: Any) -> str:
    return re.sub(r"\s*\\?\|\s*", " / ", str(value or "")).strip()


def _source_crosscheck_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    requested_source = str(data.get("requested_source") or data.get("source_label") or "요청 소스")
    requested_brand = str(data.get("requested_brand") or data.get("brand") or "")
    status = str(data.get("status") or "")
    if status in {"error", "query_failed", "unsupported", "mapping_failed", "missing", "incomplete_split"}:
        return (
            AxisFact(
                RequiredAxis.SALES_TREND,
                f"{requested_source} 미보유",
                f"{requested_source} 출처는 현재 동일 market/기간 query 경로에서 조회 성공하지 못했습니다.",
            ),
        )
    trend_rows = data.get("level_top5_trend_series")
    if isinstance(trend_rows, list) and trend_rows:
        row = _source_crosscheck_trend_row(trend_rows, requested_brand)
        if row:
            first_sales = eok_value(row.get("series", [{}])[0].get("value_억원") if isinstance(row.get("series"), list) and row.get("series") else None, None)
            latest_sales = eok_value(row.get("value_recent_억원"), row.get("value_recent"))
            first_ms = pct_value(row.get("from_ms_pct"))
            latest_ms = pct_value(row.get("to_ms_pct"))
            content = (
                f"{requested_source} {row.get('from_period')}→{row.get('to_period')} "
                f"매출 {first_sales}→{latest_sales}, MS {first_ms}→{latest_ms}"
            )
            return (AxisFact(RequiredAxis.SALES_TREND, f"{requested_source} 보유", content),)
    segments = data.get("level_segments")
    if isinstance(segments, list) and segments:
        top = next((item for item in segments if isinstance(item, dict)), {})
        if top:
            content = (
                f"{requested_source} {data.get('period') or '최신 기간'} "
                f"매출 {eok_value(top.get('value_억원'), top.get('value'))}, MS {pct_value(top.get('ms_recent_pct'))}"
            )
            return (AxisFact(RequiredAxis.SALES_TREND, f"{requested_source} 보유", content),)
    return (
        AxisFact(
            RequiredAxis.SALES_TREND,
            f"{requested_source} 미보유",
            f"{requested_source} 출처는 조회 결과 행이 없어 값을 표시하지 않습니다.",
        ),
    )


def _quarter_metric_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    brand = str(data.get("requested_brand") or data.get("brand") or "브랜드")
    row = _brand_level_segment(data, brand)
    if not row:
        return ()
    source = str(data.get("source_label") or data.get("source") or "보유 소스")
    period = str(data.get("period") or "요청 기간")
    sales = eok_value(row.get("value_억원") or row.get("value_recent_억원"), row.get("value") or row.get("value_recent"))
    share = pct_value(row.get("ms_recent_pct") or row.get("to_ms_pct"))
    rank = rank_value(row.get("rank"), None)
    parts = [
        f"{source} {period} 기준 {brand}",
        f"매출 {sales}" if sales else "",
        f"MS {share}" if share else "",
        f"순위 {rank}위" if rank else "",
    ]
    content = " ".join(part for part in parts if part)
    if not content.strip():
        return ()
    return (AxisFact(RequiredAxis.BRAND_POSITION, "브랜드 핵심 지표", content),)


def _brand_level_segment(data: RenderData, brand: str) -> dict[str, Any]:
    segments = data.get("level_segments")
    if not isinstance(segments, list):
        return {}
    for item in segments:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("brand") or item.get("name") or item.get("product") or "")
        if candidate == brand:
            return item
    return next((item for item in segments if isinstance(item, dict)), {})


def _source_crosscheck_trend_row(rows: list[Any], requested_brand: str) -> dict[str, Any]:
    if requested_brand:
        for item in rows:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("brand") or item.get("name") or item.get("product") or "")
            if candidate == requested_brand:
                return item
    return next((item for item in rows if isinstance(item, dict)), {})


def _is_hira_disease_call(call: dict[str, Any]) -> bool:
    tool = str(call.get("tool") or "")
    source = str(call.get("source") or "")
    return source == "hira_disease" or tool == "get_disease_stats" or tool.startswith("hira_disease")


def _is_hira_procedure_call(call: dict[str, Any]) -> bool:
    tool = str(call.get("tool") or "")
    source = str(call.get("source") or "")
    return source == "hira_procedure" or tool == "get_procedure_stats" or tool.startswith("hira_procedure")


def _agent_calculation_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    metric = str(data.get("metric") or "")
    if metric not in _REQUIRED_METRIC_AXES:
        return ()
    collector = _AGENT_CALCULATION_FACTS.get(metric)
    if collector is None:
        return ()
    return collector(data, str(data.get("brand") or "브랜드"))


def _brand_metric_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    metric = str(data.get("metric") or "")
    brand = str(data.get("brand") or "브랜드")
    axes = _REQUIRED_METRIC_AXES.get(metric, ())
    facts: list[AxisFact] = []
    collector = _BRAND_METRIC_FACTS.get(metric)
    if collector is not None:
        facts.extend(collector(data, brand))
    if RequiredAxis.BRAND_POSITION in axes or data.get("answer_scope") in {"single_brand_focus", "single_brand_trend"} or _is_direct_brand_metric(data):
        facts.extend(_single_brand_axis_facts(data, brand))
    if RequiredAxis.MARKET_STRUCTURE in axes or _market_structure_payload_present(data):
        facts.extend(_market_structure_axis_facts(data))
    return tuple(facts)


def _single_brand_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    facts: list[AxisFact] = []
    trend_metric = _required_sales_trend_metric(data, brand)
    if trend_metric:
        facts.append(AxisFact(RequiredAxis.SALES_TREND, "매출 추이", trend_metric))
    focus_metric = _required_single_brand_focus_metric(data, brand)
    if focus_metric:
        facts.append(AxisFact(RequiredAxis.BRAND_POSITION, "브랜드 핵심 지표", focus_metric))
    return tuple(facts)


def _market_structure_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    if _is_single_brand_scope(data):
        return ()
    rows: list[tuple[str, str]] = []
    if _prefer_top_trend_rows(data):
        rows.extend(_required_top_trend_rows(data))
    else:
        if isinstance(data.get("level_segments"), list):
            rows.extend(_required_level_segment_rows(data))
        if isinstance(data.get("level_top5_trend_series"), list):
            rows.extend(_required_top_trend_rows(data))
    return tuple(AxisFact(RequiredAxis.MARKET_STRUCTURE, label, content) for label, content in rows)


def _market_structure_payload_present(data: RenderData) -> bool:
    return isinstance(data.get("level_segments"), list) or isinstance(data.get("level_top5_trend_series"), list)


def _ordered_axis_rows(facts: list[AxisFact]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for axis in _REQUIRED_AXIS_ORDER:
        axis_facts = _prioritized_axis_facts(axis, tuple(fact for fact in facts if fact.axis == axis))
        for fact in axis_facts:
            key = (fact.label, fact.content)
            if key in seen or not fact.content:
                continue
            seen.add(key)
            rows.append(key)
    return rows


def _prioritized_axis_facts(axis: RequiredAxis, facts: tuple[AxisFact, ...]) -> tuple[AxisFact, ...]:
    if axis == RequiredAxis.SALES_TREND and any(fact.label == "시장/브랜드 변화율 대조" for fact in facts):
        return tuple(fact for fact in facts if fact.label != "매출 변화")
    if axis == RequiredAxis.MARKET_STRUCTURE and any(fact.label == "브랜드 추세 비교" for fact in facts):
        return tuple(fact for fact in facts if not _is_snapshot_rank_fact(fact))
    return facts


def _is_snapshot_rank_fact(fact: AxisFact) -> bool:
    return fact.label.endswith("상위") or (fact.label.startswith("상위 ") and fact.label.endswith(" 추이"))


def _metric_fact_detail(call: dict[str, Any], calls: list[dict[str, Any]]) -> MetricFactDetail:
    if not _market_structure_payload_present_in_call(call):
        return MetricFactDetail.FULL
    if any(_agent_metric_present(other_call, "market_vs_brand_delta") for other_call in calls):
        return MetricFactDetail.CORE_ONLY
    if any(_agent_metric_present(other_call, "brand_trend_comparison") for other_call in calls):
        return MetricFactDetail.MONTHLY_ONLY
    return MetricFactDetail.FULL


def _market_structure_payload_present_in_call(call: dict[str, Any]) -> bool:
    data = call.get("render_data")
    return call.get("tool") == "get_brand_metric" and isinstance(data, dict) and _market_structure_payload_present(data)


def _agent_metric_present(call: dict[str, Any], metric: str) -> bool:
    data = call.get("render_data")
    if call.get("tool") != "agent_calculation" or not isinstance(data, dict):
        return False
    return data.get("metric") == metric


def _is_single_brand_scope(data: dict[str, Any]) -> bool:
    """Return whether the answer must stay centered on the requested brand."""

    return data.get("answer_scope") in {"single_brand_trend", "single_brand_focus"}


def _is_direct_brand_metric(data: dict[str, Any]) -> bool:
    """Return whether direct metric facts must be echoed in the answer."""

    metric = str(data.get("metric") or "")
    if metric not in {"sales", "market_share", "share", "rank"}:
        return False
    return any(
        data.get(key) not in (None, "")
        for key in ("sales_억원", "sales_krw", "ms_recent_pct", "market_share", "rank")
    )


def _required_market_member_metric(data: dict[str, Any], brand: str) -> str:
    period = str(data.get("period") or "latest")
    rank = rank_value(data.get("rank"), None)
    ms = pct_value(data.get("ms_recent_pct"))
    sales = eok_value(data.get("sales_억원"), data.get("sales_krw"))
    parts = [
        f"{brand} 최신 시장 멤버 지표",
        f"기간 {period}",
        f"순위 {rank}위" if rank else "",
        f"시장점유율 {ms}" if ms else "",
        f"매출 {sales}" if sales else "",
    ]
    return " ".join(part for part in parts if part)


def _required_yoy_growth(data: dict[str, Any], brand: str) -> str:
    period = str(data.get("period") or "")
    from_period = str(data.get("from_period") or "").strip()
    to_period = str(data.get("to_period") or "").strip()
    period_label = period or "→".join(part for part in (from_period, to_period) if part)
    parts = [
        f"{brand} YoY",
        period_label,
        f"기준 매출 {eok_value(data.get('from_sales_억원'), data.get('from_sales_krw'))}",
        f"비교 매출 {eok_value(data.get('to_sales_억원'), data.get('to_sales_krw'))}",
        f"매출 변화 {eok_value(data.get('sales_delta_억원'), data.get('sales_delta_krw'))}",
        f"성장률 {pct_value(data.get('growth_pct'))}",
    ]
    return " ".join(part for part in parts if part)


def _required_average_share(data: dict[str, Any], brand: str) -> str:
    period = str(data.get("period") or "")
    parts = [
        f"{brand} 평균 점유율",
        period,
        pct_value(data.get("avg_ms_pct")),
    ]
    return " ".join(part for part in parts if part)


def _sales_delta_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    period = str(data.get("period") or "")
    content = (
        f"{brand} {period}: {eok_value(data.get('from_sales_억원'), data.get('from_sales_krw'))}"
        f" → {eok_value(data.get('to_sales_억원'), data.get('to_sales_krw'))}, "
        f"변화 {eok_value(data.get('sales_delta_억원'), data.get('sales_delta_krw'))}"
        f"({pct_value(data.get('sales_delta_pct'))})"
    )
    return (AxisFact(RequiredAxis.SALES_TREND, "매출 변화", content),)


def _market_share_delta_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    period = str(data.get("period") or "")
    content = (
        f"{brand} {period}: {pct_value(data.get('from_ms_pct'))}"
        f" → {pct_value(data.get('to_ms_pct'))}, 변화 {pct_value(data.get('ms_delta_pct'))}p"
    )
    return (AxisFact(RequiredAxis.MARKET_STRUCTURE, "점유율 변화", content),)


def _market_vs_brand_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    return (AxisFact(RequiredAxis.SALES_TREND, "시장/브랜드 변화율 대조", _required_market_vs_brand_delta(data, brand)),)


def _brand_trend_comparison_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    return (AxisFact(RequiredAxis.MARKET_STRUCTURE, "브랜드 추세 비교", _required_brand_trend_comparison(data, brand)),)


def _competitive_insight_axis_facts(data: RenderData, _brand: str) -> tuple[AxisFact, ...]:
    return tuple(AxisFact(RequiredAxis.MARKET_STRUCTURE, label, content) for label, content in _required_competitive_insight_rows(data))


def _portfolio_decline_axis_facts(data: RenderData) -> tuple[AxisFact, ...]:
    rows: list[AxisFact] = []
    for item in data.get("decliners", [])[:5]:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brand") or "")
        if not brand:
            continue
        period = _comparison_period(item)
        top_gainers = _portfolio_gainer_text(item.get("top_gainers"))
        content = " ".join(
            part
            for part in (
                f"{brand} {period}",
                f"MS {pct_value(item.get('from_ms_pct'))} → {pct_value(item.get('to_ms_pct'))}",
                f"변화 {_pct_point_delta(item.get('share_delta_pctp'))}",
                f"최신 매출 {eok_value(None, item.get('to_sales_krw'))}",
                f"동시장 상승 후보 {top_gainers}" if top_gainers else "",
                "직접 인과/처방 이동 단정 불가",
            )
            if part
        )
        rows.append(AxisFact(RequiredAxis.MARKET_STRUCTURE, "포트폴리오 MS 하락", content))
    return tuple(rows)


def _market_member_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    return (AxisFact(RequiredAxis.MARKET_STRUCTURE, "비교 브랜드 지표", _required_market_member_metric(data, brand)),)


def _yoy_growth_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    return (AxisFact(RequiredAxis.SALES_TREND, "YoY 성장률", _required_yoy_growth(data, brand)),)


def _average_share_axis_facts(data: RenderData, brand: str) -> tuple[AxisFact, ...]:
    return (AxisFact(RequiredAxis.MARKET_STRUCTURE, "평균 점유율", _required_average_share(data, brand)),)


def _required_single_brand_focus_metric(data: dict[str, Any], brand: str) -> str:
    period = str(data.get("period") or "")
    sales = eok_value(data.get("sales_억원"), data.get("sales_krw"))
    share = pct_value(data.get("ms_recent_pct") or data.get("market_share"))
    rank = rank_value(data.get("rank"), data.get("total_brands_in_market"))
    rank_label = f"{rank}위" if rank and "/" not in rank else rank
    if not any((sales, share, rank_label)):
        return ""
    period_label = _metric_period_label(data, period)
    parts = [
        f"{brand} {period_label}",
        f"매출 {sales}" if sales else "",
        f"시장점유율 {share}" if share else "",
        f"순위 {rank_label}" if rank_label else "",
    ]
    return " ".join(part for part in parts if part)


def _metric_period_label(data: dict[str, Any], period: str) -> str:
    requested_period = str(data.get("requested_period") or "").strip()
    fallback_period = str(data.get("fallback_period") or "").strip()
    if requested_period and fallback_period and fallback_period == period and requested_period != fallback_period:
        return f"사용 가능한 최신 기준 {period}"
    return period


def _required_sales_trend_metric(data: RenderData, brand: str) -> str:
    series = data.get("brand_value_series_10pt")
    if not isinstance(series, list):
        return ""
    points = [point for point in series if isinstance(point, dict) and point.get("period")]
    if len(points) < 2:
        return ""
    first = points[0]
    last = points[-1]
    first_sales = eok_value(first.get("value_억원"), first.get("value_krw"))
    last_sales = eok_value(last.get("value_억원"), last.get("value_krw"))
    first_ms = pct_value(first.get("ms_pct"))
    last_ms = pct_value(last.get("ms_pct"))
    parts = [
        f"{brand} 매출 시계열 {first.get('period')} {first_sales} → {last.get('period')} {last_sales}",
        f"MS {first_ms} → {last_ms}" if first_ms and last_ms else "",
    ]
    return ", ".join(part for part in parts if part)


def _required_market_vs_brand_delta(data: dict[str, Any], brand: str) -> str:
    period = str(data.get("period") or "")
    parts = [
        f"{brand} {period}",
        f"브랜드 매출 {eok_value(data.get('brand_from_sales_억원'), data.get('brand_from_sales_krw'))}"
        f" → {eok_value(data.get('brand_to_sales_억원'), data.get('brand_to_sales_krw'))}",
        f"브랜드 변화율 {pct_value(data.get('brand_delta_pct'))}",
        f"시장 매출 {eok_value(data.get('market_from_sales_억원'), data.get('market_from_sales_krw'))}"
        f" → {eok_value(data.get('market_to_sales_억원'), data.get('market_to_sales_krw'))}",
        f"시장 변화율 {pct_value(data.get('market_delta_pct'))}",
        f"변화율 차이 {pct_value(data.get('delta_pct_gap'))}p",
        _market_causal_signal(data),
    ]
    return " ".join(part for part in parts if part)


def _market_causal_signal(data: dict[str, Any]) -> str:
    relation = str(data.get("comparison_relation") or "")
    gap = _numeric(data.get("delta_pct_gap"))
    if relation == "same_direction_market_down":
        return "근거 기반 인과 분석: 시장 동반 하락이 브랜드 매출 하락의 주요 배경으로 해석됨"
    if relation == "brand_declined_more_than_market" or gap < -3:
        return "근거 기반 인과 분석: 브랜드 하락폭이 시장보다 커 브랜드 고유 압력 가능성이 큼"
    if relation == "brand_declined_less_than_market" or gap > 3:
        return "근거 기반 인과 분석: 시장 하락에도 브랜드 방어력이 상대적으로 확인됨"
    if relation == "brand_specific_weakness_signal":
        return "근거 기반 인과 분석: 시장이 버티는 동안 브랜드가 하락해 브랜드 고유 약세 신호"
    if relation == "brand_outperformed_falling_market":
        return "근거 기반 인과 분석: 시장 하락 속 브랜드는 역행해 점유 방어 신호를 보이나 직접 처방 이동은 확인 불가"
    return "근거 기반 인과 분석: 시장과 브랜드 변화율 격차로 배경 요인을 판별"


def _required_brand_trend_comparison(data: dict[str, Any], brand: str) -> str:
    comparison = str(data.get("comparison_brand") or "비교 브랜드")
    period = str(data.get("period") or "")
    parts = [
        f"{brand} vs {comparison} {period}",
        f"{brand} MS {pct_value(data.get('brand_from_ms_pct'))} → {pct_value(data.get('brand_to_ms_pct'))}",
        f"{brand} MS 변화 {pct_value(data.get('brand_share_delta_pctp'))}p",
        f"{comparison} MS {pct_value(data.get('comparison_from_ms_pct'))} → {pct_value(data.get('comparison_to_ms_pct'))}",
        f"{comparison} MS 변화 {pct_value(data.get('comparison_share_delta_pctp'))}p",
        f"{brand} 매출 변화율 {pct_value(data.get('brand_sales_delta_pct'))}",
        f"{comparison} 매출 변화율 {pct_value(data.get('comparison_sales_delta_pct'))}",
        _trend_causal_signal(data, brand, comparison),
    ]
    return " ".join(part for part in parts if part)


def _trend_causal_signal(data: dict[str, Any], brand: str, comparison: str) -> str:
    signal = str(data.get("comparison_signal") or "")
    if signal == "comparison_outpaced_anchor_trend":
        return f"근거 기반 인과 분석: {comparison}이 점유율·매출 성장에서 {brand}를 앞서며 경쟁 압력으로 작용"
    if signal == "comparison_gaining_while_anchor_flat_or_down":
        return f"근거 기반 인과 분석: {comparison} 점유 확대와 {brand} 정체/하락이 맞물려 처방 이동 후보 신호"
    return f"근거 기반 인과 분석: {brand}와 {comparison}의 추세 격차로 위협 수준을 판별"


def _required_competitive_insight_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    market_delta = eok_value(data.get("market_delta_억원"), data.get("market_delta_krw"))
    market_growth = pct_value(data.get("market_growth_pct"))
    for signal in data.get("signals", [])[:3]:
        if not isinstance(signal, dict):
            continue
        brand = str(signal.get("brand") or "")
        if not brand:
            continue
        parts = [
            brand,
            f"share-of-growth {pct_value(signal.get('share_of_growth_pct'))}" if signal.get("share_of_growth_pct") is not None else "",
            f"성장분해 시장 {market_growth}" if market_growth else "",
            f"점유 {pct_value(signal.get('share_delta_pctp'))}p" if signal.get("share_delta_pctp") is not None else "",
            f"시장 변화 {market_delta}" if market_delta else "",
            f"cohort z-score {number_value(signal.get('z_score'))}" if signal.get("z_score") is not None else "",
            f"백분위 {pct_value(signal.get('percentile'))}" if signal.get("percentile") is not None else "",
        ]
        rows.append(("인사이트 계산", " ".join(part for part in parts if part)))
    movement = _required_gain_loss_movement(data)
    if movement:
        rows.append(("인사이트 계산", movement))
    return rows


def _required_gain_loss_movement(data: dict[str, Any]) -> str:
    nested = data.get("gain_loss")
    if isinstance(nested, dict):
        gainer_brand = str(nested.get("gainer") or "")
        faller_brand = str(nested.get("faller") or "")
        if gainer_brand and faller_brand:
            period = _comparison_period(nested)
            parts = [
                f"{gainer_brand} {period} 상승폭 {pct_value(nested.get('gainer_delta_pctp'))}p",
                f"{faller_brand} {period} 하락폭 {pct_value(nested.get('faller_delta_pctp'))}p",
                "근거 기반 인과 분석: 두 브랜드 점유율이 반대 방향으로 변했으나 직접 처방 이동은 확인 불가",
            ]
            return " ".join(part for part in parts if part)
    gainer = data.get("top_gainer")
    faller = data.get("top_faller")
    if not isinstance(gainer, dict) or not isinstance(faller, dict):
        return ""
    gainer_brand = str(gainer.get("brand") or "")
    faller_brand = str(faller.get("brand") or "")
    if not gainer_brand or not faller_brand:
        return ""
    period = _comparison_period(gainer) or _comparison_period(faller) or str(data.get("period") or "")
    parts = [
        f"{gainer_brand} {period} 상승폭 {pct_value(gainer.get('share_delta_pctp'))}p",
        f"{faller_brand} {period} 하락폭 {pct_value(faller.get('share_delta_pctp'))}p",
        "근거 기반 인과 분석: 두 브랜드 점유율이 반대 방향으로 변했으나 직접 처방 이동은 확인 불가",
    ]
    return " ".join(part for part in parts if part)


def _required_level_segment_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    level = str(data.get("level") or "분석 기준")
    for item in data.get("level_segments", [])[:5]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        rank = rank_value(item.get("rank"), None)
        ms = pct_value(item.get("ms_recent_pct"))
        sales = eok_value(None, item.get("value"))
        parts = [part for part in (f"{rank}위" if rank else "", str(name), f"시장점유율 {ms}" if ms else "", f"매출 {sales}" if sales else "") if part]
        if parts:
            rows.append((f"{level} 상위", " ".join(parts)))
    return rows


def _prefer_top_trend_rows(data: dict[str, Any]) -> bool:
    """Prefer trend rows when snapshot segment values are all zero but trend facts are populated."""
    return _all_level_segment_values_zero(data) and _top_trend_values_contain_nonzero(data)


def _all_level_segment_values_zero(data: dict[str, Any]) -> bool:
    segments = data.get("level_segments")
    if not isinstance(segments, list) or not segments:
        return False
    checked = 0
    for item in segments[:5]:
        if not isinstance(item, dict):
            continue
        checked += 1
        if _numeric(item.get("ms_recent_pct")) != 0 or _numeric(item.get("value")) != 0:
            return False
    return checked > 0


def _top_trend_values_contain_nonzero(data: dict[str, Any]) -> bool:
    trends = data.get("level_top5_trend_series")
    if not isinstance(trends, list):
        return False
    for item in trends[:5]:
        if not isinstance(item, dict):
            continue
        if _numeric(item.get("ms_recent_pct")) != 0 or _numeric(item.get("value_recent_억원")) != 0 or _numeric(item.get("value_recent")) != 0:
            return True
    return False


def _numeric(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct_point_delta(value: Any) -> str:
    rendered = pct_value(value)
    return f"{rendered}p" if rendered else ""


def _pct_path(data: dict[str, Any]) -> str:
    start = pct_value(data.get("from_ms_pct"))
    end = pct_value(data.get("to_ms_pct"))
    if start and end:
        return f"{start} → {end}"
    return end or start


def _portfolio_gainer_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value[:3]:
        if not isinstance(item, dict) or not item.get("brand"):
            continue
        parts.append(
            " ".join(
                part
                for part in (
                    str(item.get("brand") or ""),
                    _pct_point_delta(item.get("share_delta_pctp")),
                )
                if part
            )
        )
    return ", ".join(part for part in parts if part)


def _required_top_trend_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    axis_label = display_level_name(data.get("level") or "Brand")
    for item in data.get("level_top5_trend_series", [])[:5]:
        if not isinstance(item, dict):
            continue
        group_value = item.get("name") or item.get("brand")
        if not group_value:
            continue
        rank = rank_value(item.get("rank"), None)
        share_delta = _top_trend_share_delta(item)
        period = _top_trend_delta_period(share_delta) or _comparison_period(item) or str(data.get("period") or "")
        sales = _top_trend_sales(item)
        sales_delta = eok_value(item.get("value_delta_억원"), item.get("value_delta_krw"))
        ms_path = _top_trend_ms_path(share_delta)
        delta_text = _top_trend_delta_text(share_delta, period)
        parts = [
            f"{rank}위" if rank else "",
            str(group_value),
            ms_path,
            delta_text,
            f"최신 매출 {sales}" if sales else "",
            f"매출 변화 {sales_delta}" if sales_delta else "",
        ]
        rows.append((f"상위 {axis_label} 추이", " ".join(part for part in parts if part)))
        monthly_ms = _top_trend_monthly_ms_summary(item)
        if monthly_ms:
            rows.append((f"상위 {axis_label} 월별 MS", monthly_ms))
    return rows


def _comparison_period(data: dict[str, Any]) -> str:
    period_from = str(data.get("period_from") or "")
    period_to = str(data.get("period_to") or "")
    if period_from and period_to:
        return f"{period_from}→{period_to}"
    series = data.get("series")
    if isinstance(series, list) and series:
        first = series[0]
        last = series[-1]
        if isinstance(first, dict) and isinstance(last, dict):
            start = str(first.get("period") or "")
            end = str(last.get("period") or "")
            if start and end:
                return f"{start}→{end}"
    return str(data.get("period") or "")


def _top_trend_share_delta(item: dict[str, Any]) -> TopTrendShareDelta:
    series = item.get("series")
    first = series[0] if isinstance(series, list) and series and isinstance(series[0], dict) else {}
    latest = series[-1] if isinstance(series, list) and series and isinstance(series[-1], dict) else {}
    from_period = str(item.get("from_period") or first.get("period") or "")
    to_period = str(item.get("to_period") or latest.get("period") or "")
    from_ms_pct = _present_value(item.get("from_ms_pct"), first.get("ms_pct"))
    to_ms_pct = _present_value(item.get("to_ms_pct"), item.get("ms_recent_pct"), latest.get("ms_pct"))
    return TopTrendShareDelta(
        from_period=from_period,
        from_ms_pct=from_ms_pct,
        to_period=to_period,
        to_ms_pct=to_ms_pct,
        delta_pctp=item.get("share_delta_pctp"),
    )


def _present_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _top_trend_delta_period(delta: TopTrendShareDelta) -> str:
    if delta.from_period and delta.to_period:
        return f"{delta.from_period}→{delta.to_period}"
    return ""


def _top_trend_ms_path(delta: TopTrendShareDelta) -> str:
    from_ms = pct_value(delta.from_ms_pct)
    to_ms = pct_value(delta.to_ms_pct)
    if from_ms and to_ms and delta.from_period and delta.to_period:
        return f"{delta.from_period} MS {from_ms} → {delta.to_period} MS {to_ms}"
    if to_ms:
        return f"최신 시장점유율 {to_ms}"
    return ""


def _top_trend_delta_is_surfaceable(delta: TopTrendShareDelta, period: str) -> bool:
    return can_surface_derived_value(
        delta.delta_pctp,
        required_period=period,
        delta_operands=DeltaOperands(
            from_value=delta.from_ms_pct,
            to_value=delta.to_ms_pct,
            delta_value=delta.delta_pctp,
        ),
    )


def _top_trend_delta_text(delta: TopTrendShareDelta, period: str) -> str:
    if _top_trend_delta_is_surfaceable(delta, period):
        return f"{period} 점유율 변화 {pct_value(delta.delta_pctp)}p"
    if delta.delta_pctp not in (None, "", "-"):
        return TOP_TREND_DELTA_WITHHELD
    return ""


def _required_hira_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str, str, str]] = set()
    candidates: list[HiraRow] = []
    missing_period = False
    for label, code, disease_name, patient_count, year in _dedupe_hira_rows(_hira_rows(data), seen=seen):
        if not can_surface_derived_value(patient_count, required_period=year):
            if patient_count not in (None, "", "-"):
                missing_period = True
            continue
        candidates.append((label, code, disease_name, patient_count, year))
    calls = data.get("calls")
    if isinstance(calls, list):
        for call in calls:
            render_data = call.get("render_data") if isinstance(call, dict) else None
            if not isinstance(render_data, dict):
                continue
            for label, code, disease_name, patient_count, year in _dedupe_hira_rows(_hira_rows(render_data), seen=seen):
                if not can_surface_derived_value(patient_count, required_period=year):
                    if patient_count not in (None, "", "-"):
                        missing_period = True
                    continue
                candidates.append((label, code, disease_name, patient_count, year))
    rows: list[tuple[str, str]] = []
    for label, code, disease_name, patient_count, year in _select_required_hira_rows(candidates):
        rows.append(("HIRA 환자수", f"{disease_name}({code}) {year}년 {label}: {patient_count}명"))
    if rows:
        return rows
    if missing_period:
        return [("HIRA 환자수", "기준기간 미확인으로 환자수 표시 보류")]
    return _required_hira_unavailable_rows(data)


def _required_hira_procedure_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    procedure_rows = _hira_procedure_rows(data)
    calls = data.get("calls")
    if isinstance(calls, list):
        for call in calls:
            render_data = call.get("render_data") if isinstance(call, dict) else None
            if isinstance(render_data, dict):
                procedure_rows.extend(_hira_procedure_rows(render_data))
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for label, code, procedure_name, patient_count, year in procedure_rows:
        if not can_surface_derived_value(patient_count, required_period=year):
            continue
        key = (str(label or ""), str(code or ""), str(year or ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append(("HIRA 진료행위", f"{procedure_name}({code}) {year}년 {label}: {patient_count}명"))
        if len(rows) >= 3:
            break
    if rows:
        return rows
    message = data.get("message")
    if message:
        return [("HIRA 진료행위", str(message))]
    return [("HIRA 진료행위", "행위코드 기준 진료행위 통계 수치 미반환")]


def _required_hira_unavailable_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    calls = data.get("calls")
    nested_calls = [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []
    mapping_by_code: dict[str, str] = {}
    unavailable_codes: set[str] = set()
    for call in nested_calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        code = str(render_data.get("sickCd") or render_data.get("mapping_sickCd") or "").strip()
        disease = str(render_data.get("disease_name") or render_data.get("mapping_disease_name") or "").strip()
        if code and disease:
            mapping_by_code.setdefault(code, disease)
        if _hira_payload_has_no_body(render_data):
            if code:
                unavailable_codes.add(code)
    rows: list[tuple[str, str]] = []
    for code in sorted(unavailable_codes):
        disease = mapping_by_code.get(code, "").strip()
        if not disease:
            continue
        rows.append(("HIRA 조회 상태", f"{code} {disease}: 환자수 수치 미반환"))
    return rows


def _hira_payload_has_no_body(data: dict[str, Any]) -> bool:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return False
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    result_code = str(header.get("resultCode") or "").strip()
    return bool(result_code and result_code != "00" and payload.get("body") is None)


def _select_required_hira_rows(rows: list[HiraRow]) -> list[HiraRow]:
    """Prefer high-level admission/outpatient rows over narrow age tails for required facts."""

    if not rows:
        return []
    priority_labels = ("외래", "입원")
    selected: list[HiraRow] = []
    for priority in priority_labels:
        selected.extend(row for row in rows if str(row[0] or "") == priority and row not in selected)
    if selected:
        return selected[:2]
    non_age = [row for row in rows if not re.match(r"^\d+_\d+세$", str(row[0] or ""))]
    return (non_age or rows)[:3]


_AGENT_CALCULATION_FACTS: Final[dict[str, RequiredFactCollector]] = {
    "sales_delta": _sales_delta_axis_facts,
    "market_share_delta": _market_share_delta_axis_facts,
    "market_vs_brand_delta": _market_vs_brand_axis_facts,
    "brand_trend_comparison": _brand_trend_comparison_axis_facts,
    "competitive_insight_signals": _competitive_insight_axis_facts,
}

_BRAND_METRIC_FACTS: Final[dict[str, RequiredFactCollector]] = {
    "market_member_snapshot": _market_member_axis_facts,
    "yoy_growth": _yoy_growth_axis_facts,
    "average_share": _average_share_axis_facts,
}


def _call_fact_block(
    call: dict[str, Any],
    *,
    detail: MetricFactDetail = MetricFactDetail.FULL,
) -> str:
    data = call.get("render_data")
    if not isinstance(data, dict):
        return ""
    tool = str(call.get("tool") or "")
    if tool == "deep_analysis_related_news":
        return _news_facts(data)
    if tool == "portfolio_decline_analysis":
        return _portfolio_decline_facts(data)
    if tool in {"get_brand_metric", "get_market_landscape", "agent_calculation", "unsupported_metric"}:
        return _metric_facts(tool, data, detail=detail)
    if _is_hira_disease_call(call):
        return _hira_facts(tool, data)
    if _is_hira_procedure_call(call):
        return _hira_procedure_facts(data)
    if "clinical" in tool:
        return _clinical_trial_facts(data)
    if "patent" in tool or "orangebook" in tool:
        return _patent_facts(data)
    if tool == "web_search" or str(call.get("source") or "") == "web_search":
        return ""
    return _generic_facts(tool, data)


def _news_facts(data: dict[str, Any]) -> str:
    rows = []
    for item in items(data):
        rows.append(
            (
                item.get("date"),
                item.get("title"),
                item.get("source"),
                item.get("url"),
                item.get("summary"),
                item.get("match_excerpt"),
            )
        )
    if not rows:
        message = data.get("message") or "관련 뉴스 없음"
        return table("### 인사이트 근거 fact - 뉴스/이슈", ("항목", "값"), (("상태", message),))
    return table("### 인사이트 근거 fact - 뉴스/이슈", ("날짜", "제목", "출처", "URL", "요약", "매칭 발췌"), tuple(rows))


def _portfolio_decline_facts(data: dict[str, Any]) -> str:
    rows: list[tuple[Any, Any, Any, Any, Any, Any, Any]] = []
    for item in data.get("decliners", [])[:TABLE_LIMIT]:
        if not isinstance(item, dict):
            continue
        rows.append(
            (
                item.get("brand"),
                item.get("market_name") or item.get("market_id"),
                _comparison_period(item),
                _pct_path(item),
                _pct_point_delta(item.get("share_delta_pctp")),
                eok_value(None, item.get("to_sales_krw")),
                _portfolio_gainer_text(item.get("top_gainers")),
            )
        )
    if not rows:
        return table("### JW 주요 브랜드 포트폴리오 fact", ("상태",), (("하락 브랜드 미확인",),))
    blocks = [
        table(
            "### JW 주요 브랜드 포트폴리오 fact",
            ("브랜드", "시장", "기간", "MS 경로", "MS 변화", "최신 매출", "동시장 상승 후보"),
            tuple(rows),
        )
    ]
    guardrail = data.get("interpretation_guardrail")
    if guardrail:
        blocks.append(table("### 포트폴리오 해석 가드레일", ("항목", "값"), (("주의", guardrail),)))
    return "\n\n".join(blocks)


def _metric_facts(
    tool: str,
    data: dict[str, Any],
    *,
    detail: MetricFactDetail = MetricFactDetail.FULL,
) -> str:
    if data.get("status") == "unsupported":
        subject = data.get("brand") or data.get("tool_name") or tool
        return table(f"### {cell(subject)} 지표 fact", ("항목", "값"), (("상태", data.get("message")),))

    subject = str(data.get("brand") or data.get("market_name") or data.get("market_id") or "시장")
    rows: list[tuple[str, Any]] = []
    _append(rows, "브랜드/시장", subject)
    _append(rows, "지표", data.get("metric"))
    _append(rows, _fact_period_row_label(data), data.get("period"))
    _append(rows, "매출", eok_value(data.get("sales_억원"), data.get("sales_krw")))
    _append(rows, "시장점유율", pct_value(data.get("ms_recent_pct", data.get("market_share"))))
    _append(rows, "순위", rank_value(data.get("rank"), data.get("total_brands_in_market")))
    rows.extend(_blocked_metric_rows(data))
    _append(rows, "시장규모", eok_value(data.get("market_size_억원"), data.get("market_size_recent_krw")))
    _append_surfaceable_cagr(rows, "브랜드 CAGR", "brand_cagr_5y_pct", data)
    _append_surfaceable_cagr(rows, "시장 CAGR", "market_cagr_5y_pct", data)
    _append_surfaceable_cagr(rows, "Excess growth", "excess_growth_pct", data)
    _append(rows, "HHI", number_value(data.get("hhi_recent", data.get("hhi"))))
    _append(rows, "기준 매출", eok_value(data.get("from_sales_억원"), data.get("from_sales_krw")))
    _append(rows, "비교 매출", eok_value(data.get("to_sales_억원"), data.get("to_sales_krw")))
    _append(rows, "매출 변화", eok_value(data.get("sales_delta_억원"), data.get("sales_delta_krw")))
    _append(rows, "매출 변화율", pct_value(data.get("sales_delta_pct")))
    _append(rows, "브랜드 변화율", pct_value(data.get("brand_delta_pct")))
    _append(rows, "시장 변화율", pct_value(data.get("market_delta_pct")))
    _append(rows, "변화율 차이", pct_value(data.get("delta_pct_gap")))
    _append(rows, "비교 브랜드", data.get("comparison_brand"))
    _append(rows, "브랜드 MS 변화", pct_value(data.get("brand_share_delta_pctp")))
    _append(rows, "비교 브랜드 MS 변화", pct_value(data.get("comparison_share_delta_pctp")))
    _append(rows, "브랜드 매출 변화율", pct_value(data.get("brand_sales_delta_pct")))
    _append(rows, "비교 브랜드 매출 변화율", pct_value(data.get("comparison_sales_delta_pct")))
    _append(rows, "YoY 성장률", pct_value(data.get("growth_pct")))
    _append(rows, "평균 점유율", pct_value(data.get("avg_ms_pct")))
    _append(rows, "기준 점유율", pct_value(data.get("from_ms_pct")))
    _append(rows, "비교 점유율", pct_value(data.get("to_ms_pct")))
    _append(rows, "점유율 변화", pct_value(data.get("ms_delta_pct")))

    blocks = [table(f"### {cell(subject)} 지표 fact", ("항목", "값"), tuple(rows))]
    include_context_tables = detail == MetricFactDetail.FULL
    level_segments = "" if _is_single_brand_scope(data) or not include_context_tables else _level_segments(data, subject)
    if level_segments:
        blocks.append(level_segments)
    brand_series = _brand_series(data, subject)
    if brand_series:
        blocks.append(brand_series)
    if not _is_single_brand_scope(data):
        top_brand_trends = _top_brand_trends(data)
        if include_context_tables and top_brand_trends:
            blocks.append(top_brand_trends)
        if detail == MetricFactDetail.MONTHLY_ONLY:
            monthly_trends = _top_brand_monthly_trends(data)
            if monthly_trends:
                blocks.append(monthly_trends)
    market_series = _market_series(data, subject)
    if market_series:
        blocks.append(market_series)
    return "\n\n".join(blocks)


def _blocked_metric_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    blocked = data.get("blocked_metric_values")
    if not isinstance(blocked, list):
        return []
    rows: list[tuple[str, str]] = []
    for item in blocked:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if message:
            rows.append(("조회 차단", message))
    return rows


def _fact_period_row_label(data: dict[str, Any]) -> str:
    period = str(data.get("period") or "").strip()
    requested_period = str(data.get("requested_period") or "").strip()
    fallback_period = str(data.get("fallback_period") or "").strip()
    if requested_period and fallback_period and fallback_period == period and requested_period != fallback_period:
        return "사용 가능한 최신 기준"
    return "기간"


def _level_segments(data: dict[str, Any], subject: str) -> str:
    if _prefer_top_trend_rows(data):
        return ""
    segments = data.get("level_segments")
    if not isinstance(segments, list):
        return ""
    level = str(data.get("level") or "분석 기준")
    rows = tuple(
            (
                rank_value(item.get("rank"), None),
                _segment_display_name(item.get("name")),
                pct_value(item.get("ms_recent_pct")),
                eok_value(None, item.get("value")),
            )
        for item in segments[:TABLE_LIMIT]
        if isinstance(item, dict)
    )
    return table(f"### {cell(subject)} {cell(level)}별 점유율 fact", ("순위", "구분", "시장점유율", "매출"), rows)


def _brand_series(data: dict[str, Any], subject: str) -> str:
    series = data.get("brand_value_series_10pt")
    if not isinstance(series, list):
        return ""
    rows = tuple(
        (item.get("period"), eok_value(item.get("value_억원"), item.get("value_krw")), pct_value(item.get("ms_pct")))
        for item in sorted(
            (item for item in series if isinstance(item, dict) and str(item.get("period") or "").strip()),
            key=lambda item: _period_sort_key(str(item.get("period") or "")),
        )
    )
    return table(f"### {cell(subject)} 매출 시계열 fact", ("기간", "매출", "MS"), rows)


def _top_brand_trends(data: dict[str, Any]) -> str:
    trends = data.get("level_top5_trend_series")
    if not isinstance(trends, list):
        return ""
    axis_label = display_level_name(data.get("level") or "Brand")
    summary_rows: list[tuple[Any, Any, Any, Any, Any, Any, Any]] = []
    axis_values: list[Any] = []
    for item in trends[:TABLE_LIMIT]:
        if not isinstance(item, dict):
            continue
        axis_values.append(item.get("name") or item.get("brand"))
        share_delta = _top_trend_share_delta(item)
        period = _top_trend_delta_period(share_delta) or _comparison_period(item) or str(data.get("period") or "")
        summary_rows.append(
            (
                rank_value(item.get("rank"), None),
                item.get("name") or item.get("brand"),
                _top_trend_ms_cell(share_delta.from_period, share_delta.from_ms_pct),
                _top_trend_ms_cell(share_delta.to_period, share_delta.to_ms_pct),
                _top_trend_delta_cell(share_delta, period),
                _top_trend_sales(item),
                eok_value(item.get("value_delta_억원"), item.get("value_delta_krw")),
            )
        )
    blocks = [
        table(
            f"### 상위 {cell(axis_label)} 점유율 추이 fact",
            ("최신 순위", axis_label, "시작 MS", "최신 MS", "MS 변화", "최신 매출", "매출 변화"),
            tuple(summary_rows),
        )
    ]
    note = dosage_combination_note(axis_label, axis_values)
    if note:
        blocks.append(note)
    monthly_rows = _top_brand_monthly_rows(trends)
    if monthly_rows:
        blocks.append(table(f"### 상위 {cell(axis_label)} 월별 MS fact", (axis_label, "기간", "MS", "매출", "순위"), tuple(monthly_rows)))
    return "\n\n".join(blocks)


def _top_trend_ms_cell(period: str, ms_pct: Any) -> str:
    ms = pct_value(ms_pct)
    if period and ms:
        return f"{period} {ms}"
    return ms


def _top_trend_delta_cell(delta: TopTrendShareDelta, period: str) -> str:
    if _top_trend_delta_is_surfaceable(delta, period):
        return _pct_point_delta(delta.delta_pctp)
    if delta.delta_pctp not in (None, "", "-"):
        return "표시 보류"
    return ""


def _top_trend_sales(item: dict[str, Any]) -> str:
    explicit = eok_value(item.get("value_recent_억원"), item.get("value_recent"))
    if explicit:
        return explicit
    series = item.get("series")
    if not isinstance(series, list):
        return ""
    dated_rows = [
        row
        for row in series
        if isinstance(row, dict) and str(row.get("period") or "").strip()
    ]
    if not dated_rows:
        return ""
    latest = sorted(dated_rows, key=lambda row: _period_sort_key(str(row.get("period") or "")))[-1]
    return eok_value(latest.get("value_억원"), latest.get("value_krw"))


def _top_brand_monthly_trends(data: dict[str, Any]) -> str:
    trends = data.get("level_top5_trend_series")
    if not isinstance(trends, list):
        return ""
    monthly_rows = _top_brand_monthly_rows(trends)
    if not monthly_rows:
        return ""
    axis_label = display_level_name(data.get("level") or "Brand")
    return table(f"### 상위 {cell(axis_label)} 월별 MS fact", (axis_label, "기간", "MS", "매출", "순위"), tuple(monthly_rows))


def _top_brand_monthly_rows(trends: list[Any]) -> list[tuple[Any, Any, Any, Any, Any]]:
    rows: list[tuple[Any, Any, Any, Any, Any]] = []
    for item in trends[:5]:
        if not isinstance(item, dict):
            continue
        brand = item.get("name") or item.get("brand")
        series = item.get("series")
        if not brand or not isinstance(series, list):
            continue
        for point in series[-TABLE_LIMIT:]:
            if not isinstance(point, dict):
                continue
            rows.append(
                (
                    brand,
                    point.get("period"),
                    pct_value(point.get("ms_pct")),
                    eok_value(point.get("value_억원"), point.get("value_krw")),
                    rank_value(point.get("rank"), None),
                )
            )
    return rows


def _top_trend_monthly_ms_summary(item: dict[str, Any]) -> str:
    brand = item.get("brand")
    series = item.get("series")
    if not brand or not isinstance(series, list):
        return ""
    points: list[str] = []
    for point in series[-TABLE_LIMIT:]:
        if not isinstance(point, dict):
            continue
        period = point.get("period")
        ms = pct_value(point.get("ms_pct"))
        if period and ms:
            points.append(f"{period} {ms}")
    if not points:
        return ""
    return f"{brand} 월별 MS: " + " → ".join(points)


def _market_series(data: dict[str, Any], subject: str) -> str:
    series = data.get("market_size_series")
    if not isinstance(series, list):
        return ""
    brand_series = data.get("brand_value_series_10pt")
    required_periods = _trend_key_periods(brand_series) if isinstance(brand_series, list) else ()
    rows = tuple(
        (item.get("period"), eok_value(item.get("value_억원"), item.get("value_krw")), pct_value(item.get("yoy_growth_pct")))
        for item in _series_with_required_periods(series, required_periods)
        if isinstance(item, dict)
    )
    return table(f"### {cell(subject)} 시장규모 시계열 fact", ("기간", "시장규모", "YoY"), rows)


def _series_with_required_periods(series: list[Any], required_periods: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    recent = [item for item in series[-TABLE_LIMIT:] if isinstance(item, dict)]
    required = [
        item
        for item in series
        if isinstance(item, dict) and str(item.get("period") or "").strip() in required_periods
    ]
    by_period: dict[str, dict[str, Any]] = {}
    for item in (*recent, *required):
        period = str(item.get("period") or "").strip()
        if period:
            by_period[period] = item
    return tuple(sorted(by_period.values(), key=lambda item: _period_sort_key(str(item.get("period") or ""))))


def _trend_key_periods(series: list[Any]) -> tuple[str, ...]:
    points = [item for item in series if isinstance(item, dict) and item.get("period")]
    if len(points) < 2:
        return tuple(str(item.get("period")) for item in points)
    sorted_points = sorted(points, key=lambda item: _period_sort_key(str(item.get("period") or "")))
    peak = max(sorted_points, key=_series_value)
    peak_index = sorted_points.index(peak)
    trough = min(sorted_points[peak_index:], key=_series_value)
    periods: list[str] = []
    for item in (sorted_points[0], peak, trough, sorted_points[-1]):
        period = str(item.get("period") or "").strip()
        if period and period not in periods:
            periods.append(period)
    return tuple(periods)


def _series_value(item: dict[str, Any]) -> float:
    eok = item.get("value_억원")
    if isinstance(eok, int | float):
        return float(eok)
    krw = item.get("value_krw") or item.get("value")
    if isinstance(krw, int | float):
        return float(krw) / 100_000_000
    return 0.0


def _period_sort_key(period: str) -> tuple[int, int, str]:
    quarter = re.fullmatch(r"(20\d{2})-?Q([1-4])", period, flags=re.IGNORECASE)
    if quarter:
        return int(quarter.group(1)), (int(quarter.group(2)) - 1) * 3 + 1, period
    month = re.fullmatch(r"(20\d{2})-(\d{2})", period)
    if month:
        return int(month.group(1)), int(month.group(2)), period
    year = re.fullmatch(r"(20\d{2})", period)
    if year:
        return int(year.group(1)), 1, period
    return 9999, 99, period


def _hira_facts(tool: str, data: dict[str, Any]) -> str:
    if tool == "hira_disease_mapping":
        rows = (("대표 질병", data.get("disease_name")), ("KCD", data.get("sickCd")), ("근거", data.get("basis")))
        return table("### HIRA 질병 매핑 fact", ("항목", "값"), rows)
    rows: list[HiraRow] = []
    rows.extend(_hira_rows(data))
    calls = data.get("calls")
    if isinstance(calls, list):
        for call in calls:
            render_data = call.get("render_data") if isinstance(call, dict) else None
            if isinstance(render_data, dict):
                rows.extend(_hira_rows(render_data))
    rows = _dedupe_hira_rows(rows)
    visible_rows = tuple(row for row in rows if can_surface_derived_value(row[3], required_period=row[4]))
    if visible_rows:
        table_rows = tuple(
            (label, code, disease_name, year, patient_count)
            for label, code, disease_name, patient_count, year in visible_rows
        )
        return table("### HIRA 질병통계 fact", ("구분", "질병코드", "질병명", "기준연도", "환자수"), table_rows[:TABLE_LIMIT])
    if any(row[3] not in (None, "", "-") for row in rows):
        return table("### HIRA 질병통계 fact", ("상태",), (("기준기간 미확인으로 환자수 표시 보류",),))
    return table("### HIRA 질병통계 fact", ("구분", "질병코드", "질병명", "기준연도", "환자수"), ())


def _hira_rows(data: dict[str, Any]) -> list[HiraRow]:
    rows: list[HiraRow] = []
    for item in items(data):
        label = item.get("inpatOpat") or item.get("age") or item.get("grade") or item.get("lcName") or item.get("sickEngNm")
        patient_count = item.get("ptntCnt") or item.get("specCnt") or "-"
        year = surface_year(data, item)
        rows.append((label, item.get("sickCd"), item.get("sickNm"), patient_count, year))
    return rows


def _hira_procedure_facts(data: dict[str, Any]) -> str:
    rows = _hira_procedure_rows(data)
    calls = data.get("calls")
    if isinstance(calls, list):
        for call in calls:
            render_data = call.get("render_data") if isinstance(call, dict) else None
            if isinstance(render_data, dict):
                rows.extend(_hira_procedure_rows(render_data))
    deduped = _dedupe_hira_procedure_rows(rows)
    if deduped:
        return table(
            "### HIRA 진료행위통계 fact",
            ("구분", "행위코드", "행위명", "기준연도", "환자수"),
            tuple((label, code, name, year, patient_count) for label, code, name, patient_count, year in deduped[:TABLE_LIMIT]),
        )
    return table("### HIRA 진료행위통계 fact", ("내용",), ((data.get("message") or "조회 결과 없음",),))


def _hira_procedure_rows(data: dict[str, Any]) -> list[HiraRow]:
    rows: list[HiraRow] = []
    for item in items(data):
        label = (
            item.get("inpatOpat")
            or item.get("ipatOpat")
            or item.get("ipatOpatDgsTpCdNm")
            or item.get("sexCdNm")
            or item.get("ageCdNm")
            or item.get("diagCdNm")
            or item.get("ykihoCdNm")
            or item.get("sex")
            or item.get("age")
            or item.get("grade")
            or item.get("lcName")
            or item.get("locNm")
        )
        code = item.get("st5Cd") or item.get("ST5_CD") or item.get("itemCd") or item.get("mdlrtActCd") or _request_value(data, "st5Cd")
        name = item.get("st5Nm") or item.get("st5CdNm") or item.get("ST5_NM") or item.get("itemNm") or item.get("mdlrtActNm") or item.get("korNm") or "-"
        patient_count = item.get("ptntCnt") or item.get("PTNT_CNT") or "-"
        year = surface_year(data, item)
        rows.append((label, code, name, patient_count, year))
    return rows


def _dedupe_hira_procedure_rows(rows: list[HiraRow]) -> list[HiraRow]:
    seen: set[tuple[str, str, str]] = set()
    out: list[HiraRow] = []
    for label, code, name, patient_count, year in rows:
        key = (str(label or ""), str(code or ""), str(year or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append((label, code, name, patient_count, year))
    return out


def _dedupe_hira_rows(
    rows: list[HiraRow],
    *,
    seen: set[tuple[str, str, str, str]] | None = None,
) -> list[HiraRow]:
    """Keep the first patient-count row for the same disease/code/breakdown label."""

    seen_keys = seen if seen is not None else set()
    deduped: list[HiraRow] = []
    for label, code, disease_name, patient_count, year in rows:
        key = (str(label or ""), str(code or ""), str(disease_name or ""), str(year or ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append((label, code, disease_name, patient_count, year))
    return deduped


def _external_items_facts(title: str, data: dict[str, Any], keys: tuple[str, ...]) -> str:
    rows = _external_fact_rows(data, keys)
    if not rows:
        rows.append((data.get("message") or data.get("summary_text") or "조회 결과 없음",))
    return table(f"### {title} fact", ("내용",), tuple(rows))


def _external_fact_rows(data: dict[str, Any], keys: tuple[str, ...]) -> list[tuple[str]]:
    rows = _external_rows_from_data(data, keys)
    calls = data.get("calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            render_data = call.get("render_data")
            if isinstance(render_data, dict):
                rows.extend(_external_rows_from_data(render_data, keys))
            elif isinstance(call.get("summary_text"), str):
                rows.append((str(call["summary_text"]),))
    return _dedupe_external_rows(rows)[:TABLE_LIMIT]


def _patent_facts(data: dict[str, Any]) -> str:
    blocks: list[str] = []
    candidate_rows = _competitor_ingredient_candidate_rows(data)
    if candidate_rows:
        blocks.append(
            table(
                "### 경쟁 성분 후보군 fact",
                ("순위", "성분", "대표 브랜드", "출처", "시장", "기간", "매출", "MS"),
                tuple(candidate_rows[:TABLE_LIMIT]),
            )
        )
    coverage_rows = _competitor_patent_coverage_rows(data)
    if coverage_rows:
        blocks.append(table("### 경쟁 성분 특허 조회 커버리지 fact", ("항목", "내용"), tuple(coverage_rows)))
    rows = _patent_rows(data)
    if not rows:
        blocks.append(table("### 특허 fact", ("내용",), ((data.get("message") or data.get("summary_text") or "조회 결과 없음",),)))
        return "\n\n".join(blocks)
    blocks.append(
        table(
            "### 특허 fact",
            ("출처", "제품/성분", "특허번호", "상태", "만료일", "권리자/출원인"),
            tuple(rows[:TABLE_LIMIT]),
        )
    )
    return "\n\n".join(blocks)


def _patent_axis_facts(data: dict[str, Any]) -> tuple[AxisFact, ...]:
    rows = _patent_rows(data)
    if not rows:
        return ()
    content = "; ".join(
        (
            f"{product}: 특허번호 {patent_no}, 상태 {status}, 만료일 {end_date}, "
            f"권리자/출원인 {owner}, 출처 {source}"
        )
        for source, product, patent_no, status, end_date, owner in rows[:TABLE_LIMIT]
    )
    return (AxisFact(RequiredAxis.PATENT_EXCLUSIVITY, "특허 fact", content),)


def _clinical_trial_facts(data: dict[str, Any]) -> str:
    rows = _clinical_trial_rows(data)
    if not rows:
        return table("### 임상시험 fact", ("내용",), ((data.get("message") or data.get("summary_text") or "조회 결과 없음",),))
    return table(
        "### 임상시험 fact",
        ("출처", "시험/식별자", "제목/제품", "상태", "단계"),
        tuple(rows[:TABLE_LIMIT]),
    )


def _web_search_facts(data: dict[str, Any]) -> str:
    rows = []
    for item in _web_search_items(data):
        rows.append((item.get("title") or "-", item.get("url") or "-", item.get("snippet") or "-"))
    if not rows:
        return table("### 웹 검색 결과 fact(미검증)", ("내용",), ((data.get("message") or "조회 결과 없음",),))
    return table("### 웹 검색 결과 fact(미검증)", ("제목", "URL", "스니펫"), tuple(rows[:TABLE_LIMIT]))


def _web_search_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    direct = list(items(data))
    if direct:
        return direct[:TABLE_LIMIT]
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    nested: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if isinstance(render_data, dict):
            nested.extend(items(render_data))
    return nested[:TABLE_LIMIT]


def _iter_external_items(data: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    source = str(data.get("tool") or data.get("source") or "external")
    for item in items(data):
        yield source, item
    payload = data.get("payload")
    if isinstance(payload, dict):
        for item in _payload_items(payload):
            if isinstance(item, dict):
                yield source, item
    calls = data.get("calls")
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_source = str(call.get("tool") or call.get("source") or "external")
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        for item in items(render_data):
            yield call_source, item
        nested_payload = render_data.get("payload")
        if isinstance(nested_payload, dict):
            for item in _payload_items(nested_payload):
                if isinstance(item, dict):
                    yield call_source, item


def _patent_rows(data: dict[str, Any]) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for source, item in _iter_external_items(data):
        patent_no = item.get("DOMESTIC_PATENT_NO") or item.get("KOR_PAT_NO")
        if not patent_no:
            continue
        product = _join_external_values(item.get("ITEM_NAME") or item.get("PRT_NAME"), item.get("INGR_NAME") or item.get("INGR_ENG_NAME"))
        status = str(item.get("DOMESTIC_PATENT_STATUS") or item.get("KOR_STATUS") or "-")
        end_date = str(item.get("DOMESTIC_END_DATE") or item.get("KOR_EXP_DATE") or "-")
        owner = str(item.get("PATENTEE") or item.get("KOR_APPLICANT") or "-")
        rows.append((source, product or "-", str(patent_no), status, end_date, owner))
    return _dedupe_external_rows(rows)


def _competitor_ingredient_candidate_rows(data: dict[str, Any]) -> list[tuple[str, str, str, str, str, str, str, str]]:
    raw = data.get("competitor_ingredient_candidates")
    if not isinstance(raw, list):
        return []
    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(
            (
                str(item.get("rank") or "-"),
                str(item.get("molecule") or "-"),
                str(item.get("brand") or "-"),
                str(item.get("source") or "-"),
                str(item.get("market") or "-"),
                str(item.get("period") or "-"),
                str(item.get("sales") or "-"),
                str(item.get("market_share") or "-"),
            )
        )
    return rows


def _competitor_patent_coverage_rows(data: dict[str, Any]) -> list[tuple[str, str]]:
    raw = data.get("competitor_patent_coverage")
    if not isinstance(raw, dict):
        return []
    rows = [
        ("출처", str(raw.get("sources") or "MFDS 의약품특허목록, FDA OrangeBook")),
        ("상태", str(raw.get("message") or "경쟁 성분 후보별 특허 조회 상태 미보유")),
        ("범위", str(raw.get("scope") or "현재 특허 DB에서 확인되는 항목만 표시하며, 전체 독점권을 단정하지 않습니다.")),
    ]
    return rows


def _clinical_trial_rows(data: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    rows.extend(_clinical_trial_direct_rows(data))
    calls = data.get("calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            render_data = call.get("render_data")
            if isinstance(render_data, dict):
                rows.extend(_clinical_trial_direct_rows(render_data))
    for source, item in _iter_external_items(data):
        row = _clinical_trial_item_row(source, item)
        if row:
            rows.append(row)
    return _dedupe_external_rows(rows)


def _clinical_trial_direct_rows(data: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    nct_ids = data.get("nct_ids")
    if not isinstance(nct_ids, list) or not nct_ids:
        return []
    title = str(data.get("briefTitle") or data.get("title") or "-")
    status = str(data.get("overallStatus") or data.get("status") or "-")
    return [(str(data.get("tool") or data.get("source") or "external"), ", ".join(str(value) for value in nct_ids[:3]), title, status, "-")]


def _clinical_trial_item_row(source: str, item: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    protocol = item.get("protocolSection")
    protocol_data = protocol if isinstance(protocol, dict) else {}
    identification = protocol_data.get("identificationModule")
    identification_data = identification if isinstance(identification, dict) else {}
    status = protocol_data.get("statusModule")
    status_data = status if isinstance(status, dict) else {}
    design = protocol_data.get("designModule")
    design_data = design if isinstance(design, dict) else {}
    trial_id = item.get("NCTId") or identification_data.get("nctId") or item.get("CLNC_TEST_SN")
    title = item.get("briefTitle") or identification_data.get("briefTitle") or identification_data.get("officialTitle") or item.get("CLINC_EXAM_TITLE") or item.get("GOODS_NAME")
    if not trial_id and not title:
        return None
    phase = item.get("phase") or item.get("CLINIC_STEP_NAME") or design_data.get("phases") or "-"
    if isinstance(phase, list):
        phase = ", ".join(str(value) for value in phase)
    trial_status = item.get("overallStatus") or status_data.get("overallStatus") or item.get("CLINC_EXAM_STTUS") or "-"
    return (source, str(trial_id or "-"), str(title or "-"), str(trial_status), str(phase))


def _join_external_values(*values: Any) -> str:
    return " / ".join(str(value) for value in values if value)


def _external_rows_from_data(data: dict[str, Any], keys: tuple[str, ...]) -> list[tuple[str]]:
    rows: list[tuple[str]] = []
    for item in items(data):
        values = [item.get(key) for key in keys if item.get(key)]
        if values:
            rows.append((" / ".join(str(value) for value in values[:3]),))
    direct = _external_direct_values(data, keys)
    if direct:
        rows.append((" / ".join(direct[:3]),))
    rows.extend(_external_clinical_rows(data))
    rows.extend(_external_payload_rows(data, keys))
    if not rows and isinstance(data.get("summary_text"), str):
        rows.append((str(data["summary_text"]),))
    return rows


def _external_direct_values(data: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values = [str(data[key]) for key in keys if data.get(key)]
    nct_ids = data.get("nct_ids")
    if isinstance(nct_ids, list) and nct_ids:
        values.insert(0, ", ".join(str(value) for value in nct_ids[:3]))
    return values


def _external_clinical_rows(data: dict[str, Any]) -> list[tuple[str]]:
    nct_ids = data.get("nct_ids")
    if not isinstance(nct_ids, list) or not nct_ids:
        return []
    title = str(data.get("briefTitle") or data.get("title") or "").strip()
    status = str(data.get("overallStatus") or data.get("status") or "").strip()
    parts = [", ".join(str(value) for value in nct_ids[:3]), title, status]
    return [(" / ".join(part for part in parts if part),)]


def _external_payload_rows(data: dict[str, Any], keys: tuple[str, ...]) -> list[tuple[str]]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return []
    rows: list[tuple[str]] = []
    for item in _payload_items(payload):
        if not isinstance(item, dict):
            continue
        values = [item.get(key) for key in keys if item.get(key)]
        if values:
            rows.append((" / ".join(str(value) for value in values[:3]),))
        clinical = _clinical_study_text(item)
        if clinical:
            rows.append((clinical,))
    return rows


def _payload_items(payload: dict[str, Any]) -> list[Any]:
    for key in ("items", "results", "studies", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    response = payload.get("response")
    if isinstance(response, dict):
        body = response.get("body")
        if isinstance(body, dict):
            items_value = body.get("items")
            if isinstance(items_value, dict):
                item = items_value.get("item")
                if isinstance(item, list):
                    return item
                if isinstance(item, dict):
                    return [item]
            if isinstance(items_value, list):
                return items_value
    return []


def _clinical_study_text(item: dict[str, Any]) -> str:
    protocol = item.get("protocolSection")
    if not isinstance(protocol, dict):
        return ""
    identification = protocol.get("identificationModule")
    status = protocol.get("statusModule")
    title = ""
    nct_id = ""
    overall_status = ""
    if isinstance(identification, dict):
        nct_id = str(identification.get("nctId") or "").strip()
        title = str(identification.get("briefTitle") or identification.get("officialTitle") or "").strip()
    if isinstance(status, dict):
        overall_status = str(status.get("overallStatus") or "").strip()
    parts = [nct_id, title, overall_status]
    return " / ".join(part for part in parts if part)


def _dedupe_external_rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = tuple(str(value).strip() for value in row)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _generic_facts(tool: str, data: dict[str, Any]) -> str:
    rows = []
    for key, value in data.items():
        if key in {"items", "payload", "calls"}:
            continue
        if isinstance(value, str | int | float):
            rows.append((key, value))
    return table(f"### {cell(tool)} fact", ("항목", "값"), tuple(rows[:TABLE_LIMIT]))


def _source_block(calls: list[dict[str, Any]], sources: list[str]) -> str:
    blocks: list[str] = [provenance_fact_markdown(calls, sources)]
    value_rows = _value_provenance_rows(calls)
    if value_rows:
        blocks.append(
            table(
                "### 수치별 출처 fact",
                ("수치", "소스", "기간", "시장정의", "축", "tool_call_id"),
                tuple(
                    (
                        row.value_label,
                        row.source,
                        row.period,
                        row.market,
                        row.axis,
                        row.tool_call_id,
                    )
                    for row in value_rows
                ),
            )
        )
    rows = _data_source_rows(calls, [] if value_rows else sources)
    rows.extend(_news_source_rows(calls))
    rows.extend(_hira_source_rows(calls))
    rows.extend(_hira_procedure_source_rows(calls))
    rows.extend(_external_source_rows(calls))
    rows.extend(_fallback_source_rows(sources, rows))
    if rows:
        blocks.append(table("### 출처 유형 fact", ("출처", "상세"), tuple(rows)))
    return "\n\n".join(blocks)


def _value_provenance_rows(calls: list[dict[str, Any]]) -> list[ValueProvenanceFact]:
    rows: list[ValueProvenanceFact] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for index, call in enumerate(calls, start=1):
        data = call.get("render_data")
        if not isinstance(data, dict) or _is_failed_metric_status(data):
            continue
        source = _value_source_label(data, call)
        if not source:
            continue
        period = _value_period(data)
        market = _value_market(data)
        axis = _value_axis(data)
        tool_call_id = _value_tool_call_id(data, index)
        for value_label in _numeric_value_labels(data):
            key = (value_label, source, period, market, axis)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                ValueProvenanceFact(
                    value_label=value_label,
                    source=source,
                    period=period,
                    market=market,
                    axis=axis,
                    tool_call_id=tool_call_id,
                )
            )
    return rows[:TABLE_LIMIT * 3]


def _is_failed_metric_status(data: dict[str, Any]) -> bool:
    return str(data.get("status") or "") in {
        "error",
        "query_failed",
        "unsupported",
        "mapping_failed",
        "missing",
        "incomplete_split",
    }


def _is_failed_external_source_status(call: dict[str, Any], data: dict[str, Any]) -> bool:
    status = str(call.get("status") or data.get("status") or "")
    return status in {
        "error",
        "query_failed",
        "unsupported",
        "mapping_failed",
        "missing",
        "incomplete_split",
        "no_data",
    }


def _value_source_label(data: dict[str, Any], call: dict[str, Any]) -> str:
    raw = str(data.get("source_label") or call.get("source") or data.get("source") or "")
    if not raw or raw == "cache":
        return ""
    label = source_label(raw)
    if label in {"IQVIA", "IQVIA NSA"}:
        return "IQVIA NSA"
    if label == "UBIST":
        return "UBIST"
    return label


def _value_period(data: dict[str, Any]) -> str:
    period = str(data.get("period") or "").strip()
    if period:
        return period
    for key in ("to_period", "fallback_period", "requested_period"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return "-"


def _value_market(data: dict[str, Any]) -> str:
    query_spec = data.get("query_spec")
    if isinstance(query_spec, dict):
        market = str(query_spec.get("market") or query_spec.get("market_id") or "").strip()
        if market:
            return market
    return str(data.get("market") or data.get("market_id") or "-")


def _value_axis(data: dict[str, Any]) -> str:
    return str(data.get("requested_axis") or data.get("level") or data.get("metric") or "-")


def _value_tool_call_id(data: dict[str, Any], index: int) -> str:
    return str(data.get("tool_call_id") or data.get("query_result_id") or f"tool_call_{index}")


def _numeric_value_labels(data: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    subject = str(data.get("brand") or data.get("market_name") or data.get("market_id") or "").strip()
    sales = eok_value(data.get("sales_억원"), data.get("sales_krw"))
    if subject and sales:
        rows.append(f"{subject} 매출 {sales}")
    share = pct_value(data.get("ms_recent_pct", data.get("market_share")))
    if subject and share:
        rows.append(f"{subject} MS {share}")
    rank = rank_value(data.get("rank"), data.get("total_brands_in_market"))
    if subject and rank:
        rows.append(f"{subject} 순위 {rank}")
    for item in _segment_compare_rows(data)[:TABLE_LIMIT]:
        name = _segment_display_name(item.get("name") or item.get("brand") or item.get("product"))
        if not name:
            continue
        item_sales = eok_value(item.get("value_억원") or item.get("value_recent_억원"), item.get("value") or item.get("value_recent"))
        if item_sales:
            rows.append(f"{name} 매출 {item_sales}")
        item_share = pct_value(item.get("ms_recent_pct") or item.get("to_ms_pct"))
        if item_share:
            rows.append(f"{name} MS {item_share}")
        item_rank = rank_value(item.get("rank"), None)
        if item_rank:
            rows.append(f"{name} 순위 {item_rank}")
    return rows


def _data_source_rows(calls: list[dict[str, Any]], sources: list[str]) -> list[tuple[str, str]]:
    labels = {source_label(source) for source in sources}
    for call in calls:
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        if _is_failed_metric_status(data):
            continue
        label = source_label(str(data.get("source_label") or call.get("source") or ""))
        if label in {"UBIST", "IQVIA", "IQVIA NSA"}:
            labels.add(label)
    data_labels = sorted(label for label in labels if label in {"UBIST", "IQVIA", "IQVIA NSA"})
    if not data_labels:
        return []
    periods = _period_range(calls)
    details: list[str] = []
    extra_details: list[str] = []
    if periods:
        details.append(f"기간 {periods}")
    query_specs = tuple(_query_specs(calls))
    market = next((str(spec.get("market") or spec.get("market_id")) for spec in query_specs if spec.get("market") or spec.get("market_id")), "")
    market_name = _first_market_scope_value(calls, query_specs, "market_name")
    view = _market_view_name(query_specs, market)
    denominator = _first_market_scope_value(calls, query_specs, "total_brands_in_market")
    market_detail = _market_detail_text(market=market, market_name=market_name, view=view, denominator=denominator)
    if market_detail:
        extra_details.append(market_detail)
    structure_detail = _market_structure_detail(calls)
    if structure_detail:
        extra_details.append(structure_detail)
    denominator_note = _market_landscape_denominator_note(
        query_specs=query_specs,
        primary_market=market,
        primary_denominator=denominator,
    )
    if denominator_note:
        extra_details.append(denominator_note)
    market_definition = _first_market_scope_value(calls, query_specs, "market_definition")
    if market_definition:
        extra_details.append(f"market_definition {market_definition}")
    if not extra_details:
        return []
    details.extend(extra_details)
    suffix = f" — {', '.join(details)}" if details else ""
    return [("데이터 상세", f"{' / '.join(data_labels)}{suffix}")]


def _market_view_name(query_specs: tuple[dict[str, Any], ...], market: str) -> str:
    explicit = next((str(spec.get("view") or spec.get("view_type")) for spec in query_specs if spec.get("view") or spec.get("view_type")), "")
    if explicit:
        return VIEW_NAME_BY_INTERNAL_LABEL.get(explicit, explicit)
    return CONFIRMED_MARKET_VIEW_BY_ID.get(market, "")


def _market_detail_text(*, market: str, market_name: str, view: str, denominator: Any) -> str:
    label = market_name or market
    if not label:
        return ""
    qualifiers: list[str] = []
    if view:
        qualifiers.append(view)
    if denominator:
        qualifiers.append(f"분모 {denominator}")
    if qualifiers:
        return f"시장: {label} ({', '.join(qualifiers)})"
    return f"시장: {label}"


def _market_structure_detail(calls: list[dict[str, Any]]) -> str:
    structure = _first_market_structure(calls)
    if not structure or str(structure.get("type") or "") != "class_split":
        return ""
    axis_label = str(structure.get("display_axis_label") or "Class 2").strip() or "Class 2"
    denominator = structure.get("display_denominator")
    axis_text = f"{axis_label} 기준"
    if denominator not in (None, ""):
        axis_text = f"{axis_text} 분모 {denominator}"
    guardrail = str(
        structure.get("comparison_guardrail")
        or "전체 market_landscape 분모와 Class 기준 분모는 직접 비교하지 않음"
    ).strip()
    return f"Class 구분 존재: 운영 노출은 {axis_text}; {guardrail}"


def _first_market_structure(calls: list[dict[str, Any]]) -> dict[str, Any]:
    for call in calls:
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        structure = data.get("market_structure")
        if isinstance(structure, dict):
            return structure
    return {}


def _market_landscape_denominator_note(
    *,
    query_specs: tuple[dict[str, Any], ...],
    primary_market: str,
    primary_denominator: Any,
) -> str:
    counterpart_market = CONFIRMED_MARKET_LANDSCAPE_COUNTERPART_BY_ID.get(primary_market)
    if not counterpart_market:
        return ""
    for spec in query_specs:
        market = str(spec.get("market") or spec.get("market_id") or "")
        if market != counterpart_market:
            continue
        denominator = spec.get("total_brands_in_market") or spec.get("denominator") or spec.get("rank_denominator")
        rank = _rank_position(spec.get("rank"))
        if not rank or denominator in (None, "") or str(denominator) == str(primary_denominator):
            continue
        return f"참고: {market} 기준 순위는 {rank}/{denominator}으로 표시될 수 있음"
    fallback_denominator = CONFIRMED_MARKET_LANDSCAPE_COUNTERPART_DENOMINATOR_BY_ID.get(primary_market)
    if fallback_denominator in (None, "") or str(fallback_denominator) == str(primary_denominator):
        return ""
    rank = next((_rank_position(spec.get("rank")) for spec in query_specs if str(spec.get("market") or spec.get("market_id") or "") == primary_market and spec.get("rank")), "")
    if not rank:
        return ""
    return f"참고: {counterpart_market} 기준 순위는 {rank}/{fallback_denominator}으로 표시될 수 있음"


def _rank_position(rank: Any) -> str:
    """Return the ordinal rank without an attached denominator."""

    value = rank_value(rank, None)
    if not value:
        return ""
    return value.split("/", 1)[0].removesuffix("위")


def _first_market_scope_value(calls: list[dict[str, Any]], specs: tuple[dict[str, Any], ...], key: str) -> Any:
    for spec in specs:
        value = spec.get(key)
        if value not in (None, ""):
            return value
    for call in calls:
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        value = data.get(key)
        if value not in (None, ""):
            return value
    return ""


def _denominator_basis(view: str, market: str, denominator: Any) -> str:
    count = str(denominator)
    if view == "market_landscape":
        basis = "market_landscape rows"
    elif view == "competitive_dynamics":
        basis = "competitive_dynamics filtered rows"
    elif view == "general_view":
        basis = "ATC4 general view"
    elif market.startswith("strategy_"):
        basis = "strategy direct competition set"
    else:
        basis = "market scope"
    if denominator in (None, ""):
        return basis
    return f"{basis} {count}개"


def _news_source_rows(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for call in calls:
        if str(call.get("source")) != "deep_analysis_events":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        tool = str(data.get("facade_tool") or call.get("tool") or "search_news")
        condition = _news_condition_text(data)
        if condition:
            rows.append(("뉴스 검색", f"events corpus · {tool} — {condition}"))
    return _dedupe_rows(rows)


def _hira_source_rows(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for call in calls:
        if str(call.get("source")) != "hira_disease" and not str(call.get("tool") or "").startswith("hira_disease"):
            continue
        facade_tool = str(call.get("tool") or "get_disease_stats")
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        nested_calls = data.get("calls")
        if isinstance(nested_calls, list):
            source_calls = [item for item in nested_calls if isinstance(item, dict)]
        else:
            source_calls = [call]
        rows.extend(_hira_source_rows_from_calls(source_calls, facade_tool))
    return _dedupe_rows(rows)


def _hira_procedure_source_rows(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for call in calls:
        if str(call.get("source")) != "hira_procedure" and str(call.get("tool") or "") != "get_procedure_stats":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        source_calls = data.get("calls") if isinstance(data.get("calls"), list) else [call]
        seen: set[tuple[str, str]] = set()
        for nested in source_calls:
            nested_data = nested.get("render_data") if isinstance(nested, dict) else None
            if not isinstance(nested_data, dict) or _is_failed_external_source_status(nested, nested_data):
                continue
            tool = str(nested.get("tool") or call.get("tool") or "get_procedure_stats")
            st5_cd = str(_request_value(nested_data, "st5Cd") or "").strip()
            year = str(_request_value(nested_data, "year") or nested_data.get("year") or "").strip()
            detail = f"HIRA 진료행위정보서비스 · {tool}"
            suffix = ", ".join(part for part in (f"st5Cd {st5_cd}" if st5_cd else "", f"{year}년" if year else "") if part)
            if suffix:
                detail += f" — {suffix}"
            row = ("외부 HIRA", detail)
            if row not in seen:
                rows.append(row)
                seen.add(row)
    return _dedupe_rows(rows)


def _hira_source_rows_from_calls(calls: list[dict[str, Any]], facade_tool: str) -> list[tuple[str, str]]:
    by_disease: dict[tuple[str, str], dict[str, set[str] | str]] = {}
    for call in calls:
        data = call.get("render_data")
        if not isinstance(data, dict) or _is_failed_external_source_status(call, data):
            continue
        tool = str(call.get("tool") or "")
        rows = _dedupe_hira_rows(_hira_rows(data))
        code = str(data.get("mapping_sickCd") or _request_value(data, "sickCd") or "").strip()
        disease_name = str(data.get("mapping_disease_name") or data.get("disease_name") or "").strip()
        if rows:
            for label, row_code, row_disease, _patient_count, _year in rows:
                actual_code = str(row_code or code).strip()
                actual_name = str(row_disease or disease_name).strip()
                if not actual_code and not actual_name:
                    continue
                entry = by_disease.setdefault((actual_code, actual_name), {"labels": set(), "years": set()})
                _add_hira_source_option(entry, tool, data, str(label or ""))
        elif code or disease_name:
            entry = by_disease.setdefault((code, disease_name), {"labels": set(), "years": set()})
            _add_hira_source_option(entry, tool, data, "")
    rows_out: list[tuple[str, str]] = []
    redundant_name_only = _redundant_hira_name_only_keys(by_disease)
    for (code, disease_name), values in by_disease.items():
        if (code, disease_name) in redundant_name_only:
            continue
        labels = values["labels"] if isinstance(values["labels"], set) else set()
        years = values["years"] if isinstance(values["years"], set) else set()
        label_text = "/".join(_ordered_hira_labels(labels)) + " 기준" if labels else ""
        year_text = f", {'/'.join(sorted(years))}년" if years else ""
        disease_text = " ".join(part for part in (code, disease_name) if part).strip()
        option_text = f" — {label_text}{year_text}" if label_text else (f" — {year_text.lstrip(', ')}" if year_text else "")
        rows_out.append(("외부 HIRA", f"HIRA 질병정보서비스 · {facade_tool} — {disease_text}{option_text}"))
    return rows_out


def _redundant_hira_name_only_keys(by_disease: dict[tuple[str, str], dict[str, set[str] | str]]) -> set[tuple[str, str]]:
    coded_names = {
        _normalize_hira_disease_name(disease_name)
        for code, disease_name in by_disease
        if code and disease_name
    }
    redundant: set[tuple[str, str]] = set()
    for code, disease_name in by_disease:
        if code or not disease_name:
            continue
        normalized = _normalize_hira_disease_name(disease_name)
        if any(normalized and (normalized in coded or coded in normalized) for coded in coded_names):
            redundant.add((code, disease_name))
    return redundant


def _normalize_hira_disease_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣]", "", value)
    return normalized.replace("원발성", "")


def _external_source_rows(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for call in calls:
        if str(call.get("source")) != "external_api":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        tool = str(data.get("facade_tool") or call.get("tool") or "external_api")
        nested_calls = data.get("calls")
        if isinstance(nested_calls, list):
            for nested in nested_calls:
                if not isinstance(nested, dict):
                    continue
                nested_data = nested.get("render_data")
                if not isinstance(nested_data, dict) or _is_failed_external_source_status(nested, nested_data):
                    continue
                request = nested_data.get("request")
                request_text = _request_text(request) if isinstance(request, dict) else ""
                nested_tool = str(nested.get("tool") or tool)
                detail = f"{nested_tool}"
                if request_text:
                    detail += f" — {request_text}"
                rows.append(("외부 API", f"{tool} · {detail}"))
    return _dedupe_rows(rows)


def _web_search_source_rows(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for call in calls:
        if str(call.get("source")) != "web_search" and str(call.get("tool") or "") != "web_search":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        provider = str(data.get("provider") or "web_search")
        query = str(data.get("query") or _request_value(data, "query") or "").strip()
        detail = f"{provider} 웹 검색 결과(미검증)"
        if query:
            detail += f" — query={query}"
        for item in _web_search_items(data):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if url:
                rows.append(("웹 검색", f"{detail} — {title} {url}".strip()))
        if not rows:
            rows.append(("웹 검색", detail))
    return _dedupe_rows(rows)


def _fallback_source_rows(sources: list[str], existing_rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    existing_text = " ".join(detail for _label, detail in existing_rows)
    rows: list[tuple[str, str]] = []
    for source in sorted(set(sources)):
        label = source_label(source)
        if label and label not in existing_text:
            rows.append((label, "-"))
    return rows


def _news_condition_text(data: dict[str, Any]) -> str:
    applied_filters = data.get("applied_filters")
    if isinstance(applied_filters, dict):
        parts = [_news_filter_label(str(key), value) for key, value in applied_filters.items() if value]
        if parts:
            return "검색조건 " + ", ".join(str(part) for part in parts)
    filter_entries = data.get("filter_entries")
    if isinstance(filter_entries, list | tuple):
        parts = []
        for entry in filter_entries:
            if isinstance(entry, list | tuple) and len(entry) >= 2:
                parts.append(f"{entry[0]}={entry[1]}")
        if parts:
            return "검색조건 " + ", ".join(str(part) for part in parts)
    transparency_parts = []
    for key in ("title_contains", "text_contains", "content_contains"):
        value = data.get(key)
        if value:
            transparency_parts.append(f"{key}={value}")
    return "검색조건 " + ", ".join(transparency_parts) if transparency_parts else ""


def _news_filter_label(key: str, value: Any) -> str:
    labels = {
        "title_contains": "제목",
        "text_contains": "본문/제목",
        "content_contains": "본문",
        "relevance_brands": "관련 브랜드",
        "recent_days": "최근 일수",
        "date_from": "시작일",
        "date_to": "종료일",
        "category": "분류",
        "min_impact_score": "최소 영향도",
        "limit": "건수",
    }
    return f"{labels.get(key, key)}={value}"


def _period_range(calls: list[dict[str, Any]]) -> str:
    periods = sorted(set(_period_values(calls)))
    if not periods:
        return ""
    if len(periods) == 1:
        return periods[0]
    return f"{periods[0]}~{periods[-1]}"


def _period_values(value: Any) -> list[str]:
    periods: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "period" and isinstance(item, str) and re.fullmatch(r"20\d{2}-(?:\d{2}|Q[1-4])", item):
                periods.append(item)
            elif isinstance(item, dict | list | tuple):
                periods.extend(_period_values(item))
    elif isinstance(value, list | tuple):
        for item in value:
            periods.extend(_period_values(item))
    return periods


def _query_specs(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for call in calls:
        data = call.get("render_data")
        if isinstance(data, dict) and isinstance(data.get("query_spec"), dict):
            specs.append(data["query_spec"])
        if isinstance(data, dict):
            inferred = {
                key: value
                for key, value in {
                    "source": data.get("source_label") or call.get("source"),
                    "view": data.get("view") or data.get("view_type"),
                    "market": data.get("market") or data.get("market_id"),
                    "market_name": data.get("market_name"),
                    "rank": data.get("rank"),
                    "total_brands_in_market": data.get("total_brands_in_market")
                    or data.get("denominator")
                    or data.get("rank_denominator")
                    or data.get("market_brand_count"),
                }.items()
                if value
            }
            if inferred:
                specs.append(inferred)
    return specs


def _request_value(data: dict[str, Any], key: str) -> Any:
    return request_value(data, key)


def _add_hira_source_option(entry: dict[str, set[str] | str], tool: str, data: dict[str, Any], row_label: str) -> None:
    option = _hira_tool_option(tool, row_label)
    if option:
        labels = entry["labels"]
        if isinstance(labels, set):
            labels.add(option)
    year = _request_value(data, "year") or data.get("year")
    if year:
        years = entry["years"]
        if isinstance(years, set):
            years.add(str(year))


def _hira_tool_option(tool: str, row_label: str) -> str:
    if tool == "hira_disease_name_code":
        return ""
    if tool == "hira_disease_hospitalization_outpatient_stats":
        return row_label if row_label in {"입원", "외래"} else "입원/외래"
    if tool == "hira_disease_gender_age_stats":
        return ""
    if tool == "hira_disease_institution_class_stats":
        return ""
    if tool == "hira_disease_area_stats":
        return ""
    return row_label


def _request_text(request: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(request.items()) if value not in (None, ""))


def _dedupe_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, detail in rows:
        key = (label, detail)
        if key in seen:
            continue
        seen.add(key)
        out.append((label, detail))
    return out


def _ordered_hira_labels(labels: set[str]) -> list[str]:
    preferred = ("입원", "외래")
    ordered = [label for label in preferred if label in labels]
    ordered.extend(sorted(label for label in labels if label not in set(preferred)))
    return ordered


def _append(rows: list[tuple[str, Any]], label: str, value: Any) -> None:
    if value not in (None, ""):
        rows.append((label, value))


def _append_surfaceable_cagr(rows: list[tuple[str, Any]], label: str, key: str, data: dict[str, Any]) -> None:
    value = data.get(key)
    if can_surface_derived_value(value, cagr_operands=cagr_operands_from_data(data, key)):
        _append(rows, label, pct_value(value))
