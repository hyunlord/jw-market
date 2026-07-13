from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TypeAlias

from jw_chat_agent_poc.agent_loop.population_specs import StrictQueryPlan, strict_query_plan
from jw_chat_agent_poc.agent_loop.routing import should_use_agent_loop
from jw_chat_agent_poc.service.charts import _chart_intent


QuerySpec: TypeAlias = dict[str, object]


GOLDEN_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Q01", "리바로 관련 최근 이슈"),
    ("Q02", "리바로하이 환자수+매출"),
    ("Q04", "리바로 매출 추이"),
    ("Q05", "리바로 경쟁 구도 변화"),
    ("Q06", "페린젝트 매출 추이"),
    ("Q07", "리바로 채널별로 보여줘"),
    ("Q07_CHANNEL", "리바로 채널"),
    ("Q07_CHANNEL_SALES", "리바로 채널별 매출"),
    ("Q07_CHANNEL_SHARE", "리바로와 아토젯 채널별 점유율"),
    ("Q07_CHANNEL_ALIAS", "리바로 의원/병원별 실적"),
    ("Q07_CHANNEL_PARTNER", "리바로 채널 파트너"),
    ("Q07_YOUTUBE", "리바로 유튜브 채널"),
    ("STRICT_CAUSAL_NEWS", "리바로 뉴스가 매출에 영향 준 원인"),
    ("STRICT_NHI", "리바로 급여/비급여 매출 구성과 추이"),
    ("STRICT_YOY", "리바로 작년 동기 대비 매출"),
    ("STRICT_AVG_SHARE", "리바로의 지난 6개월 평균 점유율은?"),
    ("STRICT_MOLECULE_CHANNEL", "리바로 의원 채널에서 성분별 점유율"),
    ("STRICT_OXGX", "리바로 시장 오리지널 vs 제네릭 비중"),
    ("STRICT_SPECIALTY", "리바로 진료과별 경쟁사"),
    ("STRICT_FORM", "리바로 제형별 매출 추이(최근 1년)"),
    ("STRICT_COMPANY", "리바로 시장에서 급매출 회사 top3와 그 성분"),
    ("EXTERNAL_CLINICAL", "리바로 성분 임상시험 현황"),
    ("EXTERNAL_PATENT", "리바로 특허 알려줘"),
    ("DRUG_INFO", "리바로 허가정보 식약처"),
    ("SIMPLE_SALES", "리바로 매출"),
    ("SIMPLE_TREND", "리바로 매출 추이"),
    ("RANK", "리바로 점유율 순위"),
    ("TOP_BRANDS", "리바로 시장에서 상위 브랜드 뭐 있어"),
)


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    id: str
    question: str
    brand: str
    should_use_agent_loop: bool
    service_path_no_documents: str
    strict_query_plan: dict[str, object] | None
    strict_tool_names: tuple[str, ...]
    chart_intent_empty_answer: tuple[str, ...]


def test_routing_public_output_matches_legacy_snapshot() -> None:
    """Given golden questions, registry routing must preserve the legacy public output."""

    current = [_snapshot(key, question, strict_query_plan, should_use_agent_loop) for key, question in GOLDEN_QUESTIONS]
    legacy = [_snapshot(key, question, _legacy_strict_query_plan, _legacy_should_use_agent_loop) for key, question in GOLDEN_QUESTIONS]

    assert _snapshot_json(current) == _snapshot_json(legacy)


def test_news_sales_impact_is_not_rejected_before_fact_backfill() -> None:
    question = "리바로 관련 뉴스가 최근 매출에 미친 영향"
    assert strict_query_plan(question, "리바로") is None
    assert should_use_agent_loop(question) is True


def test_market_concentration_uses_deterministic_agent_loop() -> None:
    assert should_use_agent_loop("리바로 시장 집중도는 어때?") is True


def _snapshot(
    key: str,
    question: str,
    strict_plan_fn,
    route_fn,
) -> RoutingSnapshot:
    brand = "리바로"
    plan = strict_plan_fn(question, brand)
    use_agent_loop = route_fn(question)
    return RoutingSnapshot(
        id=key,
        question=question,
        brand=brand,
        should_use_agent_loop=use_agent_loop,
        service_path_no_documents="direct_agent_loop" if use_agent_loop else "legacy_chat_agent",
        strict_query_plan=_plan_dict(plan),
        strict_tool_names=_strict_tool_names(plan),
        chart_intent_empty_answer=tuple(sorted(_chart_intent(question, ""))),
    )


def _snapshot_json(snapshots: list[RoutingSnapshot]) -> str:
    return json.dumps([asdict(snapshot) for snapshot in snapshots], ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plan_dict(plan: StrictQueryPlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return {
        "specs": list(plan.specs),
        "unsupported_message": plan.unsupported_message,
        "exclusive": plan.exclusive,
        "needs_top_competitor_specialty": plan.needs_top_competitor_specialty,
        "needs_company_molecule": plan.needs_company_molecule,
    }


def _strict_tool_names(plan: StrictQueryPlan | None) -> tuple[str, ...]:
    if plan is None:
        return ()
    if plan.unsupported_message:
        return ("unsupported_metric",)
    names = ["get_brand_metric" for _ in plan.specs]
    if plan.needs_top_competitor_specialty:
        names.append("get_brand_metric")
    if plan.needs_company_molecule:
        names.append("get_brand_metric")
    return tuple(names)


def _legacy_should_use_agent_loop(question: str) -> bool:
    if _legacy_strict_query_plan(question, "리바로") is not None:
        return True
    if not any(token in question for token in (*_METRIC_TOKENS, *_EXTERNAL_TOKENS, *_DRUG_INFO_TOKENS)):
        return False
    if any(token in question for token in _DRUG_INFO_TOKENS):
        return True
    if _issue_question_needs_quant_context(question):
        return True
    if _patient_sales_question(question):
        return True
    if any(token in question for token in ("점유율", "순위")) and not _segment_metric_question(question):
        return True
    if any(token in question for token in _COMPLEX_TOKENS):
        return True
    return False


def _legacy_strict_query_plan(question: str, brand: str) -> StrictQueryPlan | None:
    channel = _requested_channel(question)
    if _asks_causal_news_sales(question):
        return StrictQueryPlan(unsupported_message="뉴스와 매출의 인과 효과는 현재 mart 지표만으로 단정할 수 없습니다.")
    if _asks_nhi(question):
        return StrictQueryPlan(unsupported_message="nhi_type dimension absent in strategic mart for this market.")
    if _asks_yoy(question):
        return StrictQueryPlan(specs=(_spec("product", metric="growth", derive=("yoy",), filters={"brand": brand}),))
    if _asks_average_share(question):
        return StrictQueryPlan(specs=(_spec("product", metric="share", derive=("average",), filters={"brand": brand, "periods": "6"}),))
    if channel and "성분" in question:
        return StrictQueryPlan(specs=(_spec("molecule", metric="share", filters={"channel": channel}),))
    if "채널별" in question and "점유율" in question:
        specs = [_spec("channel", metric="share", filters={"brand": brand})]
        if "아토젯" in question:
            specs.append(_spec("channel", metric="share", filters={"brand": "아토젯"}))
        return StrictQueryPlan(specs=tuple(specs))
    if _asks_channel_distribution(question, brand):
        return StrictQueryPlan(specs=(_spec("channel", metric="sales", filters={"brand": brand}),))
    if any(token in question for token in ("오리지널", "제네릭", "Original", "Generic")):
        return StrictQueryPlan(specs=(_spec("ox_gx", metric="share"),))
    if "진료과" in question:
        return StrictQueryPlan(needs_top_competitor_specialty=True)
    if _asks_form_sales_trend(question):
        return StrictQueryPlan(specs=(_spec("dosage_form", metric="sales", group_by=("dosage_form", "period"), derive=("trend",), filters={"periods": "12"}),))
    if "회사" in question:
        return StrictQueryPlan(specs=(_spec("company", metric="sales", limit=3),), needs_company_molecule=True)
    return None


def _spec(
    dimension: str,
    *,
    metric: str,
    group_by: tuple[str, ...] | None = None,
    derive: tuple[str, ...] = (),
    filters: dict[str, object] | None = None,
    limit: int = 10,
) -> QuerySpec:
    return {
        "source": "ubist",
        "view": "market_landscape",
        "dimensions": [dimension],
        "group_by": list(group_by or (dimension,)),
        "metrics": [metric],
        "derive": list(derive),
        "filters": filters or {},
        "limit": limit,
    }


def _requested_channel(question: str) -> str:
    for channel in CHANNEL_ALIASES:
        if channel in question:
            return channel
    return ""


def _asks_channel_distribution(question: str, brand: str) -> bool:
    if any(token in question for token in NON_ANALYTIC_CHANNEL_TERMS):
        return False
    if any(token in question for token in CHANNEL_DISTRIBUTION_TERMS):
        return True
    if "채널" in question and brand in question:
        return True
    return bool(_requested_channel(question)) and any(token in question for token in ("별", *CHANNEL_QUESTION_TERMS))


def _asks_nhi(question: str) -> bool:
    return any(token in question for token in ("급여", "비급여", "nhi", "NHI"))


def _asks_yoy(question: str) -> bool:
    return any(token in question for token in ("작년 동기", "전년 동기", "YoY", "yoy"))


def _asks_average_share(question: str) -> bool:
    return "평균" in question and "점유율" in question


def _asks_causal_news_sales(question: str) -> bool:
    return any(token in question for token in ("영향", "원인", "왜")) and any(token in question for token in ("뉴스", "이슈")) and "매출" in question


def _asks_form_sales_trend(question: str) -> bool:
    return ("제형" in question or "class" in question) and any(token in question for token in ("매출", "추이", "최근 1년"))


def _issue_question_needs_quant_context(question: str) -> bool:
    return any(token in question for token in ("최근 이슈", "관련 이슈", "이슈 뭐", "이슈 알려"))


def _patient_sales_question(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("환자", "환자수", "환자 수", "질병", "질환", "HIRA"))


def _segment_metric_question(question: str) -> bool:
    return any(token in question for token in ("제형", "성분", "진료과", "채널", "회사", "오리지널", "제네릭"))


CHANNEL_ALIASES = ("의원", "종병", "병원", "상급종병", "약국")
CHANNEL_DISTRIBUTION_TERMS = ("채널별", "채널 별", "채널로", "채널 보여", "채널 분포", "채널 mix", "채널 MIX", "채널 구성", "유통 채널")
CHANNEL_QUESTION_TERMS = ("어느", "어디", "잘 팔", "많이", "매출", "판매", "실적")
NON_ANALYTIC_CHANNEL_TERMS = ("채널 파트너", "유튜브 채널", "마케팅 채널", "홍보 채널")
_METRIC_TOKENS = ("매출", "점유율", "순위", "시장", "경쟁사", "경쟁", "상위", "위협")
_EXTERNAL_TOKENS = ("뉴스", "이슈", "HIRA", "환자", "질병", "질환", "임상", "특허", "라벨", "FDA")
_DRUG_INFO_TOKENS = ("허가", "품목", "식약처", "MFDS", "의약품정보", "의약품 정보")
_COMPLEX_TOKENS = ("같은 시장에서", "제일 큰", "가장 큰", "대비", "변화", "비교", "같이", "한번에", "함께", "하락", "떨어", "감소", "줄", "위협", "오르는", "동안", "상위")
