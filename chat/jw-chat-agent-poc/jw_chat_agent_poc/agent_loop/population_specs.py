from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Final, TypeAlias

from jw_chat_agent_poc.agentic.sales_filter_aliases import CHANNEL_ALIASES, match_channel_in_text


QuerySpec: TypeAlias = dict[str, object]


@dataclass(frozen=True, slots=True)
class StrictQueryPlan:
    """Deterministic query specs for population-sensitive questions."""

    specs: tuple[QuerySpec, ...] = ()
    metadata: tuple[dict[str, str], ...] = ()
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
CSD_ACTIVITY_TERMS: Final[tuple[str, ...]] = ("영업활동", "영업 활동", "상기 콜", "콜 수", "콜수", "활동량")
NON_ANALYTIC_CHANNEL_TERMS: Final[tuple[str, ...]] = ("채널 파트너", "유튜브 채널", "마케팅 채널", "홍보 채널")


def strict_query_plan(question: str, brand: str) -> StrictQueryPlan | None:
    """Map filter/dimension/aggregation questions to catalog-valid query specs."""

    channel = _requested_channel(question)
    for rule in STRICT_QUERY_RULES:
        plan = rule.build(question, brand, channel)
        if plan is not None:
            return plan
    return None


def _nhi_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_nhi(question):
        return StrictQueryPlan(unsupported_message="nhi_type dimension absent in strategic mart for this market.")
    return None


def _source_crosscheck_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    sources = _requested_sources(question)
    if len(sources) < 2 or not _asks_source_crosscheck(question):
        return None
    specs: list[QuerySpec] = []
    metadata: list[dict[str, str]] = []
    for source_label, source in sources:
        specs.append(
            _spec(
                "product",
                source=source,
                metric="sales",
                group_by=("product", "period"),
                derive=("trend",),
                filters={"periods": "10"},
                limit=20,
            )
        )
        metadata.append(
            {
                "contract_intent": "source_crosscheck",
                "requested_source": source_label,
                "requested_brand": brand,
            }
        )
    return StrictQueryPlan(specs=tuple(specs), metadata=tuple(metadata))


def _segment_compare_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    axes = _requested_segment_axes(question)
    if not axes or not _asks_segment_compare(question):
        return None
    specs: list[QuerySpec] = []
    metadata: list[dict[str, str]] = []
    for axis, dimension in axes:
        specs.append(_spec(dimension, source="", metric="sales", limit=5))
        metadata.append({"contract_intent": "segment_compare", "requested_axis": axis, "requested_dimension": dimension})
    return StrictQueryPlan(specs=tuple(specs), metadata=tuple(metadata))


def _quarter_metric_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    period = _requested_quarter_period(question)
    if not period or not _asks_quarter_metric(question):
        return None
    requested_sources = _requested_sources(question)
    if len(requested_sources) > 1:
        return None
    source = requested_sources[0][1] if requested_sources else ""
    metric = "share" if any(token in question for token in ("점유율", "MS", "ms", "M/S")) else "sales"
    return StrictQueryPlan(
        specs=(
            _spec(
                "product",
                source=source,
                metric=metric,
                filters={"brand": brand, "period": period},
                limit=10,
            ),
        ),
        metadata=({"contract_intent": "quarter_metric", "requested_brand": brand},),
    )


def _yoy_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_yoy(question):
        return StrictQueryPlan(specs=(_spec("product", metric="growth", derive=("yoy",), filters={"brand": brand}),))
    return None


def _average_share_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_average_share(question):
        return StrictQueryPlan(
            specs=(
                _spec(
                    "product",
                    source="",
                    metric="share",
                    derive=("average",),
                    filters={"brand": brand, "periods": "6"},
                ),
            )
        )
    return None


def _channel_molecule_plan(question: str, _brand: str, channel: str) -> StrictQueryPlan | None:
    if channel and "성분" in question:
        return StrictQueryPlan(specs=(_spec("molecule", metric="share", filters={"channel": channel}),))
    return None


def _channel_top_brand_plan(question: str, _brand: str, channel: str) -> StrictQueryPlan | None:
    if not channel or "상위" not in question or "브랜드" not in question:
        return None
    return StrictQueryPlan(
        specs=(
            _spec(
                "product",
                metric="share",
                filters={"channel": channel},
                limit=5,
            ),
        ),
    )


def _specific_channel_metric_plan(question: str, brand: str, channel: str) -> StrictQueryPlan | None:
    if not channel or any(token in question for token in ("채널별", "채널 별")):
        return None
    if any(f"{alias}별" in question or f"{alias} 별" in question for alias in CHANNEL_ALIASES):
        return None
    if not any(token in question for token in ("매출", "판매")):
        return None
    return StrictQueryPlan(
        specs=(
            _spec(
                "product",
                metric="sales",
                filters={"brand": brand, "channel": channel},
                limit=1,
            ),
        ),
    )


def _unknown_specific_channel_plan(question: str, brand: str, channel: str) -> StrictQueryPlan | None:
    if channel:
        return None
    match = re.search(r"([A-Za-z0-9가-힣_+-]+)\s*채널(?:에서|의|로)", question)
    if match is None or match.group(1) == brand:
        return None
    label = match.group(1)
    if label in {"어느", "어떤", "무슨", "마케팅", "홍보", "유튜브"}:
        return None
    return StrictQueryPlan(
        unsupported_message=(
            f"{label} 채널은 현재 지원하지 않습니다. "
            "지원 채널은 상급종병, 종병, 병원, 의원, 보건소, 기타입니다."
        )
    )


def _channel_share_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if "채널별" in question and "점유율" in question:
        specs = [_spec("channel", source="", metric="share", filters={"brand": brand})]
        if "아토젯" in question:
            specs.append(_spec("channel", source="", metric="share", filters={"brand": "아토젯"}))
        return StrictQueryPlan(specs=tuple(specs))
    return None


def _channel_distribution_plan(question: str, brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_channel_distribution(question, brand):
        return StrictQueryPlan(specs=(_spec("channel", source="", metric="sales", filters={"brand": brand}),))
    return None


def _origin_generic_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if any(token in question for token in ("오리지널", "제네릭", "Original", "Generic")):
        return StrictQueryPlan(specs=(_spec("ox_gx", metric="share"),))
    return None


def _specialty_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if "진료과" in question:
        return StrictQueryPlan(specs=(_spec("specialty", source="", metric="sales", filters={"brand": _brand}),))
    return None


def _dosage_form_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if _asks_form_sales_trend(question):
        return StrictQueryPlan(specs=(_spec("dosage_form", metric="sales", group_by=("dosage_form", "period"), derive=("trend",), filters={"periods": "12"}),))
    return None


def _company_plan(question: str, _brand: str, _channel: str) -> StrictQueryPlan | None:
    if any(token in question for token in CSD_ACTIVITY_TERMS):
        return None
    if "회사" in question:
        return StrictQueryPlan(specs=(_spec("company", metric="sales", limit=3),), needs_company_molecule=True)
    return None


STRICT_QUERY_RULES: Final[tuple[StrictQueryRule, ...]] = (
    StrictQueryRule("nhi_unsupported", _nhi_plan),
    StrictQueryRule("source_crosscheck", _source_crosscheck_plan),
    StrictQueryRule("segment_compare", _segment_compare_plan),
    StrictQueryRule("quarter_metric", _quarter_metric_plan),
    StrictQueryRule("yoy_product_growth", _yoy_plan),
    StrictQueryRule("average_product_share", _average_share_plan),
    StrictQueryRule("channel_molecule_share", _channel_molecule_plan),
    StrictQueryRule("channel_top_brands", _channel_top_brand_plan),
    StrictQueryRule("specific_channel_metric", _specific_channel_metric_plan),
    StrictQueryRule("unknown_specific_channel", _unknown_specific_channel_plan),
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
    source: str = "ubist",
    metric: str,
    group_by: tuple[str, ...] | None = None,
    derive: tuple[str, ...] = (),
    filters: dict[str, object] | None = None,
    limit: int = 10,
) -> QuerySpec:
    return {
        "source": source,
        "view": "market_landscape",
        "dimensions": [dimension],
        "group_by": list(group_by or (dimension,)),
        "metrics": [metric],
        "derive": list(derive),
        "filters": filters or {},
        "limit": limit,
    }


def _requested_channel(question: str) -> str:
    return match_channel_in_text(question) or ""


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


def _asks_form_sales_trend(question: str) -> bool:
    return ("제형" in question or "class" in question) and any(token in question for token in ("매출", "추이", "최근 1년"))


def _asks_segment_compare(question: str) -> bool:
    if any(token in question for token in ("세그먼트별", "세그먼트 별", "segment별", "Segment별")):
        return True
    if "비교" in question and len(_requested_segment_axes(question)) >= 2:
        return True
    return "비교" in question and any(token in question for token in ("Class", "Molecule", "브랜드", "용량", "제형"))


def _asks_quarter_metric(question: str) -> bool:
    return any(token in question for token in ("매출", "점유율", "MS", "ms", "M/S"))


def _requested_quarter_period(question: str) -> str:
    compact = question.replace(" ", "")
    match = re.search(r"(20\d{2})-?Q([1-4])", compact, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    match = re.search(r"(20\d{2})년?([1-4])분기", compact)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    return ""


def _requested_segment_axes(question: str) -> tuple[tuple[str, str], ...]:
    axis_map: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (("Class", "class", "클래스"), "Class", "class_2"),
        (("Molecule", "molecule", "성분"), "Molecule", "molecule"),
        (("브랜드", "Brand", "brand"), "브랜드", "product"),
        (("용량", "Dose", "dose"), "용량", "dose"),
        (("제형", "Form", "form"), "제형", "dosage_form"),
    )
    requested: list[tuple[str, str]] = []
    for tokens, axis, dimension in axis_map:
        if any(token in question for token in tokens):
            requested.append((axis, dimension))
    return tuple(requested)


def _asks_source_crosscheck(question: str) -> bool:
    return any(token in question for token in ("교차", "출처별", "출처 별", "source", "Source")) and any(
        token in question for token in ("UBIST", "ubist", "IQVIA", "iqvia")
    )


def _requested_sources(question: str) -> tuple[tuple[str, str], ...]:
    sources: list[tuple[str, str]] = []
    if any(token in question for token in ("UBIST", "ubist")):
        sources.append(("UBIST", "ubist"))
    if any(token in question for token in ("IQVIA", "iqvia")):
        sources.append(("IQVIA", "iqvia_nsa"))
    return tuple(sources)
