from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.agent_loop.periods import AgentPeriodGrounding, build_period_grounding
from jw_chat_agent_poc.common.timing import trace_span
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError


@dataclass(frozen=True, slots=True)
class QuerySlots:
    brands: tuple[str, ...]
    metric: str
    period: str
    axis: str | None
    limit: int
    history_points: int
    answer_mode: str


@dataclass(frozen=True, slots=True)
class StructuredPlan:
    kind: str
    slots: QuerySlots
    decision: AgentDecision


@dataclass(frozen=True, slots=True)
class MetricSpec:
    name: str
    owner: str
    pattern: re.Pattern[str]
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerModeSpec:
    name: str
    pattern: re.Pattern[str]


_METRICS: Final[tuple[MetricSpec, ...]] = (
    MetricSpec(
        "market_top",
        "market",
        re.compile(r"상위\s*\d*|top\s*\d*", re.IGNORECASE),
        ("get_top_brands", "get_brand_series"),
    ),
    MetricSpec(
        "brand_share",
        "brand",
        re.compile(r"점유율|시장점유율|\bMS\b", re.IGNORECASE),
        ("get_brand_share", "get_brand_sales", "get_brand_series", "get_top_brands"),
    ),
    MetricSpec(
        "brand_sales",
        "brand",
        re.compile(r"매출|처방조제액|실적"),
        ("get_brand_sales", "get_brand_share", "get_brand_series", "get_top_brands"),
    ),
    MetricSpec(
        "brand_growth",
        "brand",
        re.compile(r"성장률|증감률|성장"),
        ("get_brand_series", "get_brand_sales", "get_top_brands"),
    ),
    MetricSpec(
        "brand_rank",
        "brand",
        re.compile(r"순위|몇\s*위|랭킹|rank", re.IGNORECASE),
        ("get_brand_share", "get_brand_series", "get_top_brands"),
    ),
    MetricSpec(
        "market_structure",
        "market",
        re.compile(r"HHI|CR5|시장\s*구조|집중도", re.IGNORECASE),
        ("get_top_brands", "get_brand_series"),
    ),
    MetricSpec("market_size", "market", re.compile(r"시장\s*규모|시장규모"), ("get_brand_series", "get_top_brands")),
)
_AXES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    ("channel", re.compile(r"채널|channel", re.IGNORECASE), "get_brand_channel_breakdown"),
    ("specialty", re.compile(r"진료과|specialty", re.IGNORECASE), "get_brand_specialty_breakdown"),
)
_ANSWER_MODES: Final[tuple[AnswerModeSpec, ...]] = (
    AnswerModeSpec("explanatory", re.compile(r"왜|원인|이유|영향", re.IGNORECASE)),
    AnswerModeSpec("forecast", re.compile(r"전망|예측|향후", re.IGNORECASE)),
)
_MARKET_STATUS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)시장\s+(?:경쟁\s+)?상황"
    r"(?:\s+(?:알려\s*줘|어때(?:요)?|설명해\s*줘))?[?!.]?\s*$",
    re.IGNORECASE,
)
_TOOL_ARGUMENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "get_brand_sales": ("brand", "period"),
    "get_brand_share": ("brand", "period"),
    "get_brand_series": ("brand", "period", "history_points"),
    "compare_brands_series": ("brand", "comparison_brand"),
    "get_top_brands": ("brand", "limit"),
    "get_brand_channel_breakdown": ("brand",),
    "get_brand_specialty_breakdown": ("brand",),
}


def deterministic_market_planner_enabled() -> bool:
    return os.environ.get("CHAT_DETERMINISTIC_MARKET_PLANNER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def structured_metric_owner(question: str) -> str | None:
    """Return the owner of the first metric selected by the canonical planner."""

    metric = next((item for item in _METRICS if item.pattern.search(question)), None)
    if metric is not None:
        return metric.owner
    return "market" if is_market_status_intent(question) else None


def is_market_status_intent(question: str) -> bool:
    """Match the established market-status utterance without claiming news intent."""

    return _MARKET_STATUS_PATTERN.search(question.strip()) is not None


def plan_structured_market_question(
    question: str,
    resolver: BrandResolver,
    grounding: AgentPeriodGrounding,
    schemas: tuple[dict[str, object], ...],
) -> StructuredPlan | None:
    if not deterministic_market_planner_enabled():
        return None
    answer_mode = next(
        (spec.name for spec in _ANSWER_MODES if spec.pattern.search(question)),
        "descriptive",
    )
    if answer_mode != "descriptive":
        return None
    brands = _resolved_brands(question, resolver)
    if not brands:
        return None
    available_tools = _schema_names(schemas)
    axis = next((item for item in _AXES if item[1].search(question)), None)
    metric = next((item for item in _METRICS if item.pattern.search(question)), None)
    if metric is not None and metric.owner == "market":
        brands = brands[:1]
    comparison = len(brands) > 1
    if axis is None and metric is None and not comparison:
        return None
    history_points = _relative_history_points(question)
    period = "latest" if history_points is not None else _period(question, grounding)
    limit = _limit(question)
    if comparison:
        kind = "brand_comparison"
        tools = ("compare_brands_series", "get_brand_series", "get_top_brands")
    elif axis is not None:
        kind = f"brand_{axis[0]}"
        tools = (axis[2], "get_brand_series", "get_top_brands")
    else:
        assert metric is not None
        kind = metric.name
        if history_points is not None and metric.owner == "brand":
            tools = ("get_brand_series",)
        else:
            tools = (
                ("get_brand_sales",)
                if _is_exact_period_sales_question(question, metric, grounding)
                else metric.tools
            )
    if not set(tools).issubset(available_tools):
        return None
    slots = QuerySlots(
        brands=brands,
        metric=kind,
        period=period,
        axis=axis[0] if axis else None,
        limit=limit,
        history_points=history_points or 10,
        answer_mode=answer_mode,
    )
    calls = tuple(_call(tool, slots) for tool in tools)
    return StructuredPlan(kind=kind, slots=slots, decision=AgentDecision(tool_calls=calls))


def preflight_structured_market_question(question: str, resolver: BrandResolver) -> StructuredPlan | None:
    from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
    from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog

    with trace_span("period_grounding", "structured preflight period normalization", category="planner"):
        grounding = build_period_grounding(question)
    with trace_span("preflight_brand_resolution", "structured preflight brand resolution", category="planner"):
        brands = _resolved_brands(question, resolver)
    with trace_span("tool_schema_catalog", "structured preflight tool schema and query catalog", category="planner"):
        schemas = tool_schemas(brands, grounding.schema_periods, default_catalog())
    with trace_span("structured_plan_assembly", "structured preflight plan assembly", category="planner"):
        return plan_structured_market_question(question, resolver, grounding, schemas)


def _resolved_brands(question: str, resolver: BrandResolver) -> tuple[str, ...]:
    try:
        return tuple(item.canonical_brand for item in resolver.resolve_many(question, allow_default=False))
    except (UnsupportedBrandError, OSError):
        return ()


def _schema_names(schemas: tuple[dict[str, object], ...]) -> frozenset[str]:
    names: set[str] = set()
    for schema in schemas:
        function = schema.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return frozenset(names)


def _period(question: str, grounding: AgentPeriodGrounding) -> str:
    explicit = re.search(r"20\d{2}-(?:0[1-9]|1[0-2])", question)
    if explicit and explicit.group(0) in grounding.schema_periods:
        return explicit.group(0)
    return grounding.pre_resolved_periods[0] if grounding.pre_resolved_periods else "latest"


def _limit(question: str) -> int:
    match = re.search(r"(?:상위|top)\s*(\d{1,2})", question, re.IGNORECASE)
    return max(1, min(int(match.group(1)), 20)) if match else 5


def _relative_history_points(question: str) -> int | None:
    match = re.search(r"최근\s*(\d{1,2})\s*(년|개월|달)", question)
    if match is None:
        return None
    count = int(match.group(1))
    months = count * 12 if match.group(2) == "년" else count
    return months if 2 <= months <= 60 else None


def _is_exact_period_sales_question(
    question: str,
    metric: MetricSpec,
    grounding: AgentPeriodGrounding,
) -> bool:
    if metric.name != "brand_sales" or not grounding.pre_resolved_periods:
        return False
    return not any(
        token in question
        for token in (
            "비교",
            "각각",
            "추이",
            "변화",
            "증감",
            "최근",
            "대비",
            "차이",
            "점유율",
            "시장규모",
            "순위",
        )
    )


def _call(tool: str, slots: QuerySlots) -> ToolCallPlan:
    values = {
        "brand": slots.brands[0],
        "comparison_brand": slots.brands[1] if len(slots.brands) > 1 else "",
        "period": slots.period,
        "limit": str(slots.limit),
        "history_points": str(slots.history_points),
    }
    arguments = {field: values[field] for field in _TOOL_ARGUMENT_FIELDS[tool]}
    return ToolCallPlan(name=tool, arguments=arguments, reason=f"deterministic slots: {slots.metric}")
