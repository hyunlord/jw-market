from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.agent_loop.periods import AgentPeriodGrounding, build_period_grounding
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError


@dataclass(frozen=True, slots=True)
class QuerySlots:
    brands: tuple[str, ...]
    metric: str
    period: str
    axis: str | None
    limit: int
    answer_mode: str


@dataclass(frozen=True, slots=True)
class StructuredPlan:
    kind: str
    slots: QuerySlots
    decision: AgentDecision


@dataclass(frozen=True, slots=True)
class MetricSpec:
    name: str
    pattern: re.Pattern[str]
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerModeSpec:
    name: str
    pattern: re.Pattern[str]


_METRICS: Final[tuple[MetricSpec, ...]] = (
    MetricSpec(
        "brand_share",
        re.compile(r"점유율|시장점유율|\bMS\b", re.IGNORECASE),
        ("get_brand_share", "get_brand_sales", "get_brand_series", "get_top_brands"),
    ),
    MetricSpec(
        "brand_sales",
        re.compile(r"매출|처방조제액|실적"),
        ("get_brand_sales", "get_brand_share", "get_brand_series", "get_top_brands"),
    ),
    MetricSpec(
        "brand_growth",
        re.compile(r"성장률|증감률|성장"),
        ("get_brand_series", "get_brand_sales", "get_top_brands"),
    ),
    MetricSpec(
        "brand_rank",
        re.compile(r"순위|랭킹|rank", re.IGNORECASE),
        ("get_brand_share", "get_brand_series", "get_top_brands"),
    ),
    MetricSpec(
        "market_top",
        re.compile(r"상위\s*\d*|top\s*\d*", re.IGNORECASE),
        ("get_top_brands", "get_brand_series"),
    ),
    MetricSpec(
        "market_structure",
        re.compile(r"HHI|CR5|시장\s*구조|집중도", re.IGNORECASE),
        ("get_top_brands", "get_brand_series"),
    ),
    MetricSpec("market_size", re.compile(r"시장\s*규모|시장규모"), ("get_brand_series", "get_top_brands")),
)
_AXES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    ("channel", re.compile(r"채널|channel", re.IGNORECASE), "get_brand_channel_breakdown"),
    ("specialty", re.compile(r"진료과|specialty", re.IGNORECASE), "get_brand_specialty_breakdown"),
)
_ANSWER_MODES: Final[tuple[AnswerModeSpec, ...]] = (
    AnswerModeSpec("explanatory", re.compile(r"왜|원인|이유|영향", re.IGNORECASE)),
    AnswerModeSpec("forecast", re.compile(r"전망|예측|향후", re.IGNORECASE)),
)
_TOOL_ARGUMENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "get_brand_sales": ("brand", "period"),
    "get_brand_share": ("brand", "period"),
    "get_brand_series": ("brand", "period"),
    "compare_brands_series": ("brand", "comparison_brand"),
    "get_top_brands": ("brand", "limit"),
    "get_brand_channel_breakdown": ("brand",),
    "get_brand_specialty_breakdown": ("brand",),
}


def deterministic_market_planner_enabled() -> bool:
    return os.environ.get("CHAT_DETERMINISTIC_MARKET_PLANNER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


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
    comparison = len(brands) > 1
    if axis is None and metric is None and not comparison:
        return None
    period = _period(question, grounding)
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
        tools = metric.tools
    if not set(tools).issubset(available_tools):
        return None
    slots = QuerySlots(
        brands=brands,
        metric=kind,
        period=period,
        axis=axis[0] if axis else None,
        limit=limit,
        answer_mode=answer_mode,
    )
    calls = tuple(_call(tool, slots) for tool in tools)
    return StructuredPlan(kind=kind, slots=slots, decision=AgentDecision(tool_calls=calls))


def preflight_structured_market_question(question: str, resolver: BrandResolver) -> StructuredPlan | None:
    from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
    from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog

    grounding = build_period_grounding(question)
    brands = _resolved_brands(question, resolver)
    schemas = tool_schemas(brands, grounding.schema_periods, default_catalog())
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


def _call(tool: str, slots: QuerySlots) -> ToolCallPlan:
    values = {
        "brand": slots.brands[0],
        "comparison_brand": slots.brands[1] if len(slots.brands) > 1 else "",
        "period": slots.period,
        "limit": str(slots.limit),
    }
    arguments = {field: values[field] for field in _TOOL_ARGUMENT_FIELDS[tool]}
    return ToolCallPlan(name=tool, arguments=arguments, reason=f"deterministic slots: {slots.metric}")
