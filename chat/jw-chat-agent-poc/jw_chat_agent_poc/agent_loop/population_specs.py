from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, TypeAlias


QuerySpec: TypeAlias = dict[str, object]


@dataclass(frozen=True, slots=True)
class StrictQueryPlan:
    """Deterministic query specs for population-sensitive questions."""

    specs: tuple[QuerySpec, ...] = ()
    unsupported_message: str = ""
    exclusive: bool = True
    needs_top_competitor_specialty: bool = False
    needs_company_molecule: bool = False


StrictPlanBuilder: TypeAlias = Callable[[str, str, str], StrictQueryPlan | None]


@dataclass(frozen=True, slots=True)
class StrictQueryRule:
    """Ordered registry row for deterministic population query routing."""

    name: str
    build: StrictPlanBuilder


CHANNEL_ALIASES: Final[tuple[str, ...]] = ("의원", "종병", "병원", "상급종병", "약국")
CHANNEL_DISTRIBUTION_TERMS: Final[tuple[str, ...]] = (
    "채널별",
    "채널 별",
    "채널로",
    "채널 보여",
    "채널 분포",
    "채널 mix",
    "채널 MIX",
    "채널 구성",
    "유통 채널",
)
CHANNEL_QUESTION_TERMS: Final[tuple[str, ...]] = ("어느", "어디", "잘 팔", "많이", "매출", "판매", "실적")
NON_ANALYTIC_CHANNEL_TERMS: Final[tuple[str, ...]] = ("채널 파트너", "유튜브 채널", "마케팅 채널", "홍보 채널")


def strict_query_plan(question: str, brand: str) -> StrictQueryPlan | None:
    """Map filter/dimension/aggregation questions to catalog-valid query specs."""

    channel = _requested_channel(question)
    for rule in STRICT_QUERY_RULES:
        plan = rule.build(question, brand, channel)
        if plan is not None:
            return plan
    return None


def _causal_news_sales_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_causal_news_sales(question):
        return StrictQueryPlan(unsupported_message="뉴스와 매출의 인과 효과는 현재 mart 지표만으로 단정할 수 없습니다.")
    return None


def _nhi_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_nhi(question):
        return StrictQueryPlan(unsupported_message="nhi_type dimension absent in strategic mart for this market.")
    return None


def _yoy_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_yoy(question):
        return StrictQueryPlan(specs=(_spec("product", metric="growth", derive=("yoy",), filters={"brand": brand}),))
    return None


def _average_share_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_average_share(question):
        return StrictQueryPlan(specs=(_spec("product", metric="share", derive=("average",), filters={"brand": brand, "periods": "6"}),))
    return None


def _channel_molecule_plan(question: str, _brand: str, channel: str) -> StrictQueryPlan | None:
    if channel and "성분" in question:
        return StrictQueryPlan(specs=(_spec("molecule", metric="share", filters={"channel": channel}),))
    return None


def _channel_share_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if "채널별" in question and "점유율" in question:
        specs = [_spec("channel", metric="share", filters={"brand": brand})]
        if "아토젯" in question:
            specs.append(_spec("channel", metric="share", filters={"brand": "아토젯"}))
        return StrictQueryPlan(specs=tuple(specs))
    return None


def _channel_distribution_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_channel_distribution(question, brand):
        return StrictQueryPlan(specs=(_spec("channel", metric="sales", filters={"brand": brand}),))
    return None


def _origin_generic_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if any(token in question for token in ("오리지널", "제네릭", "Original", "Generic")):
        return StrictQueryPlan(specs=(_spec("ox_gx", metric="share"),))
    return None


def _specialty_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if "진료과" in question:
        return StrictQueryPlan(needs_top_competitor_specialty=True)
    return None


def _dosage_form_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_form_sales_trend(question):
        return StrictQueryPlan(specs=(_spec("dosage_form", metric="sales", group_by=("dosage_form", "period"), derive=("trend",), filters={"periods": "12"}),))
    return None


def _company_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if "회사" in question:
        return StrictQueryPlan(specs=(_spec("company", metric="sales", limit=3),), needs_company_molecule=True)
    return None


STRICT_QUERY_RULES: Final[tuple[StrictQueryRule, ...]] = (
    StrictQueryRule("causal_news_sales_unsupported", _causal_news_sales_plan),
    StrictQueryRule("nhi_unsupported", _nhi_plan),
    StrictQueryRule("yoy_product_growth", _yoy_plan),
    StrictQueryRule("average_product_share", _average_share_plan),
    StrictQueryRule("channel_molecule_share", _channel_molecule_plan),
    StrictQueryRule("channel_share", _channel_share_plan),
    StrictQueryRule("channel_distribution_sales", _channel_distribution_plan),
    StrictQueryRule("origin_generic_share", _origin_generic_plan),
    StrictQueryRule("specialty_top_competitor", _specialty_plan),
    StrictQueryRule("dosage_form_sales_trend", _dosage_form_plan),
    StrictQueryRule("company_sales", _company_plan),
)


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
