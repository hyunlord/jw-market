from __future__ import annotations

from dataclasses import dataclass
from typing import Final, TypeAlias


QuerySpec: TypeAlias = dict[str, object]


@dataclass(frozen=True, slots=True)
class StrictQueryPlan:
    """Deterministic query specs for population-sensitive questions."""

    specs: tuple[QuerySpec, ...] = ()
    unsupported_message: str = ""
    exclusive: bool = True
    needs_top_competitor_specialty: bool = False
    needs_company_molecule: bool = False


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
