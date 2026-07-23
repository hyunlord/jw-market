from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.agent_loop.bq_contracts import BqContract, contract_for
from jw_chat_agent_poc.agent_loop.metric_intent import explicit_base_metrics_from_question
from jw_chat_agent_poc.agent_loop.bq_slots import (
    BqSlots,
    contract_id_for_slots,
    extract_bq_slots,
    requested_prescription_metric,
)
from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.agent_loop.periods import AgentPeriodGrounding, build_period_grounding
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError


@dataclass(frozen=True, slots=True)
class BqPlan:
    contract: BqContract
    slots: BqSlots
    decision: AgentDecision
    missing_sources: tuple[str, ...] = ()


_SOURCE_VARIANTS: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("A1", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("A1", "get_brand_channel_breakdown"): ("ubist",),
    ("A2", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("A3", "get_brand_sales"): ("ubist", "iqvia_nsa"),
    ("A3", "get_brand_share"): ("ubist", "iqvia_nsa"),
    ("B1", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("B1", "get_top_brands"): ("ubist", "iqvia_nsa"),
    ("B2", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("B2", "get_top_brands"): ("ubist", "iqvia_nsa"),
    ("B3", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("B3", "get_top_brands"): ("ubist", "iqvia_nsa"),
    ("C1", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("C1", "get_brand_sales"): ("ubist", "iqvia_nsa"),
    ("C2", "get_brand_channel_breakdown"): ("ubist",),
    ("C2", "get_brand_specialty_breakdown"): ("ubist",),
    ("C2", "get_brand_series"): ("ubist",),
    ("C3", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("D2", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("E2", "get_brand_series"): ("ubist", "iqvia_nsa"),
    ("E2", "get_top_brands"): ("ubist", "iqvia_nsa"),
}

_PERIOD_TOOLS = frozenset({"get_brand_sales", "get_brand_share", "get_brand_series"})


def plan_bq_question(
    question: str,
    resolver: BrandResolver,
    grounding: AgentPeriodGrounding,
    schemas: tuple[dict[str, object], ...],
    available_sources: tuple[str, ...] | None = None,
) -> BqPlan | None:
    brands = _resolved_brands(question, resolver)
    if not brands:
        return None
    slots = extract_bq_slots(
        question,
        brand=brands[0],
        period=_period(question, grounding),
    )
    contract_id = contract_id_for_slots(slots)
    contract = contract_for(contract_id or "")
    if contract is None or not set(contract.tools).issubset(_schema_names(schemas)):
        return None
    calls = tuple(
        call
        for tool in contract.tools
        for call in _tool_calls(contract, tool, slots, available_sources)
    )
    expected_sources = tuple(
        dict.fromkeys(
            source
            for tool in contract.tools
            for source in _SOURCE_VARIANTS.get((contract.contract_id, tool), ())
            if source
        )
    )
    missing_sources = (
        tuple(source for source in expected_sources if source not in available_sources)
        if available_sources is not None
        else ()
    )
    return BqPlan(
        contract=contract,
        slots=slots,
        decision=AgentDecision(tool_calls=calls),
        missing_sources=missing_sources,
    )


def preflight_bq_question(question: str, resolver: BrandResolver) -> BqPlan | None:
    from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
    from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog

    grounding = build_period_grounding(question)
    brands = _resolved_brands(question, resolver)
    schemas = tool_schemas(brands, grounding.schema_periods, default_catalog())
    return plan_bq_question(question, resolver, grounding, schemas)


def _tool_calls(
    contract: BqContract,
    tool: str,
    slots: BqSlots,
    available_sources: tuple[str, ...] | None,
) -> tuple[ToolCallPlan, ...]:
    variants = _SOURCE_VARIANTS.get((contract.contract_id, tool), ("",))
    if available_sources is not None:
        variants = tuple(source for source in variants if not source or source in available_sources)
    explicit_metrics = explicit_base_metrics_from_question(slots.question)
    mixed_sales_volume = {"sales", "prescription_volume"}.issubset(explicit_metrics)
    calls = [
        ToolCallPlan(
            name=tool,
            arguments=_arguments(
                tool,
                slots,
                source,
                forced_measure="sales" if mixed_sales_volume and tool in _MEASURE_TOOLS else None,
            ),
            reason=f"BQ contract {contract.contract_id}",
        )
        for source in variants
    ]
    if (
        mixed_sales_volume
        and tool in _MEASURE_TOOLS
        and "ubist" in variants
    ):
        calls.append(
            ToolCallPlan(
                name=tool,
                arguments=_arguments(tool, slots, "ubist", forced_measure="prescription_volume"),
                reason=f"BQ contract {contract.contract_id}: UBIST prescription volume",
            )
        )
    return tuple(calls)


_MEASURE_TOOLS = frozenset(
    {
        "get_metric",
        "get_brand_series",
        "get_top_brands",
        "get_brand_channel_breakdown",
        "get_brand_specialty_breakdown",
    }
)


def _arguments(
    tool: str,
    slots: BqSlots,
    source: str,
    *,
    forced_measure: str | None = None,
) -> dict[str, str]:
    arguments = {"brand": slots.brand}
    if forced_measure is not None:
        arguments["measure"] = forced_measure
    elif (
        requested_prescription_metric(slots.question) == "prescription_volume"
        and tool in _MEASURE_TOOLS
    ):
        arguments["measure"] = "prescription_volume"
    if tool in _PERIOD_TOOLS:
        arguments["period"] = slots.period
    if tool == "get_top_brands":
        arguments["limit"] = "5"
    if tool == "get_brand_series":
        arguments["history_points"] = str(_relative_history_points(slots.question) or 60)
    if tool in {"search_news", "web_search"}:
        arguments["query"] = slots.question
    if source:
        arguments["source"] = source
    return arguments


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
    if _relative_history_points(question) is not None:
        return "latest"
    explicit = re.search(r"20\d{2}-(?:0[1-9]|1[0-2])", question)
    if explicit and explicit.group(0) in grounding.schema_periods:
        return explicit.group(0)
    return grounding.pre_resolved_periods[0] if grounding.pre_resolved_periods else "latest"


def _relative_history_points(question: str) -> int | None:
    match = re.search(r"최근\s*(\d{1,2})\s*(년|개월|달)", question)
    if match is None:
        return None
    count = int(match.group(1))
    months = count * 12 if match.group(2) == "년" else count
    return months if 2 <= months <= 60 else None
