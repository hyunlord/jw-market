from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final

from scripts.compose_ab_poc.catalog import CompositionCatalog
from scripts.compose_ab_poc.models import Approach


TOOL_ALIASES: Final[dict[str, str]] = {
    "compute_*": "compute_series",
    "compute_market_share": "compute_share",
    "compute_share_delta": "compute_delta",
    "compute_delta_share": "compute_delta",
    "compute_yoy": "compute_growth",
    "compute_qoq": "compute_growth",
    "compute_cagr": "compute_growth",
    "compute_top_n": "compute_rank",
    "compute_top_n_ms_trend": "compute_series",
    "compute_brand_pair_trend": "compute_series",
    "compute_concentration": "compute_hhi",
}

DIMENSION_ALIASES: Final[dict[str, str]] = {
    "brand": "product",
    "brand_name": "product",
    "brand_nm": "product",
    "product_name": "product",
    "company_name": "company",
    "manufacturer": "company",
    "class": "dosage_form",
    "class_label": "dosage_form",
    "form": "dosage_form",
    "formulation": "dosage_form",
    "specialty_data": "specialty",
    "molecule_desc": "molecule",
    "month": "period",
    "date": "period",
    "period_ym": "period",
}

METRIC_ALIASES: Final[dict[str, str]] = {
    "raw_value": "sales",
    "value": "sales",
    "sales_value": "sales",
    "market_value": "sales",
    "amount": "sales",
    "ms": "share",
    "market_share": "share",
    "share_pct": "share",
    "rank_no": "rank",
    "yoy": "growth",
    "qoq": "growth",
    "cagr": "growth",
}

DERIVE_ALIASES: Final[dict[str, str]] = {
    "market_share": "share",
    "share_pct": "share",
    "share_delta": "delta",
    "sales_delta": "delta",
    "growth_rate": "growth",
    "yoy_growth": "growth",
    "cagr": "growth",
    "sales_trend": "trend",
    "share_trend": "trend",
    "top": "top_n",
    "topn": "top_n",
    "top_5": "top_n",
    "concentration_index": "concentration",
    "unsupported": "unsupported_dimension",
}

SORT_ALIASES: Final[dict[str, str]] = {
    "value_desc": "sales_desc",
    "raw_value_desc": "sales_desc",
    "sales_value_desc": "sales_desc",
    "market_share_desc": "share_desc",
    "share_pct_desc": "share_desc",
    "rank": "rank_asc",
}


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """Grounded JSON plan and schema evidence."""

    plan: dict[str, Any]
    changes: tuple[str, ...]
    raw_errors: tuple[str, ...]
    final_errors: tuple[str, ...]


def ground_plan(parsed: dict[str, Any], approach: Approach, catalog: CompositionCatalog) -> GroundingResult:
    """Normalize LLM identifiers against the catalog and return schema evidence."""

    raw_errors = tuple(schema_errors(parsed, approach, catalog))
    plan = deepcopy(parsed)
    changes: list[str] = []
    if approach == "primitive":
        _ground_primitive(plan, catalog, changes)
    else:
        _ground_query_spec(plan, catalog, changes)
    return GroundingResult(plan, tuple(changes), raw_errors, tuple(schema_errors(plan, approach, catalog)))


def schema_errors(parsed: dict[str, Any], approach: Approach, catalog: CompositionCatalog) -> list[str]:
    """Return schema enum violations for a raw or grounded LLM plan."""

    errors: list[str] = []
    if not isinstance(parsed.get("intent_id"), str):
        errors.append("missing intent_id")
    if approach == "primitive":
        return [*errors, *_primitive_errors(parsed, catalog)]
    return [*errors, *_query_errors(parsed, catalog)]


def _ground_primitive(plan: dict[str, Any], catalog: CompositionCatalog, changes: list[str]) -> None:
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        raw_tool = step.get("tool")
        if not isinstance(raw_tool, str):
            continue
        tool = _tool_alias(raw_tool)
        if tool != raw_tool:
            changes.append(f"steps[{index}].tool: {raw_tool} -> {tool}")
            step["tool"] = tool
        if tool not in catalog.primitive_tools and tool.startswith("compute_"):
            changes.append(f"steps[{index}].tool: {tool} -> compute_series")
            step["tool"] = "compute_series"


def _ground_query_spec(plan: dict[str, Any], catalog: CompositionCatalog, changes: list[str]) -> None:
    spec = plan.get("spec")
    if not isinstance(spec, dict):
        return
    _replace_scalar(spec, "source", {"UBIST": "ubist", "Ubist": "ubist"}, changes)
    _replace_scalar(spec, "view", {"strategic": "market_landscape", "ml": "market_landscape"}, changes)
    _ground_list(spec, "dimensions", DIMENSION_ALIASES, catalog.dimensions, changes)
    _ground_list(spec, "group_by", DIMENSION_ALIASES, catalog.group_by, changes)
    _ground_list(spec, "metrics", METRIC_ALIASES, catalog.metrics, changes)
    _ground_list(spec, "derive", DERIVE_ALIASES, catalog.derivations, changes)
    sort = spec.get("sort")
    if isinstance(sort, str):
        grounded = SORT_ALIASES.get(sort, sort)
        if grounded != sort:
            changes.append(f"spec.sort: {sort} -> {grounded}")
            spec["sort"] = grounded


def _replace_scalar(target: dict[str, Any], key: str, aliases: dict[str, str], changes: list[str]) -> None:
    value = target.get(key)
    if isinstance(value, str) and value in aliases:
        replacement = aliases[value]
        changes.append(f"spec.{key}: {value} -> {replacement}")
        target[key] = replacement


def _ground_list(target: dict[str, Any], key: str, aliases: dict[str, str], allowed: tuple[str, ...], changes: list[str]) -> None:
    values = target.get(key, [])
    if not isinstance(values, list):
        return
    grounded: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            continue
        replacement = aliases.get(value, value)
        if replacement != value:
            changes.append(f"spec.{key}[{index}]: {value} -> {replacement}")
        grounded.append(replacement)
    target[key] = _dedupe(grounded, allowed)


def _dedupe(values: list[str], allowed: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value in result:
            continue
        result.append(value)
    return result


def _primitive_errors(parsed: dict[str, Any], catalog: CompositionCatalog) -> list[str]:
    steps = parsed.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["missing steps"]
    errors: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {index} not object")
            continue
        if step.get("tool") not in catalog.primitive_tools:
            errors.append(f"step {index} unknown tool {step.get('tool')!r}")
    return errors


def _query_errors(parsed: dict[str, Any], catalog: CompositionCatalog) -> list[str]:
    spec = parsed.get("spec")
    if not isinstance(spec, dict):
        return ["missing spec"]
    errors: list[str] = []
    if spec.get("source") != catalog.source:
        errors.append(f"spec source not {catalog.source}")
    if spec.get("view") != catalog.view:
        errors.append(f"spec view not {catalog.view}")
    if spec.get("market") != catalog.market:
        errors.append(f"spec market not {catalog.market}")
    errors.extend(_unknown_values(spec, "dimensions", catalog.dimensions))
    errors.extend(_unknown_values(spec, "group_by", catalog.group_by))
    errors.extend(_unknown_values(spec, "metrics", catalog.metrics))
    errors.extend(_unknown_values(spec, "derive", catalog.derivations))
    sort = spec.get("sort")
    if sort is not None and sort not in catalog.sorts:
        errors.append(f"sort unknown {sort!r}")
    return errors


def _unknown_values(spec: dict[str, Any], key: str, allowed: tuple[str, ...]) -> list[str]:
    values = spec.get(key, [])
    if not isinstance(values, list):
        return [f"{key} not list"]
    unknown = [value for value in values if value not in allowed]
    return [f"{key} unknown {unknown}"] if unknown else []


def _tool_alias(raw_tool: str) -> str:
    if raw_tool in TOOL_ALIASES:
        return TOOL_ALIASES[raw_tool]
    if "hhi" in raw_tool or "concentration" in raw_tool:
        return "compute_hhi"
    if "growth" in raw_tool or "yoy" in raw_tool or "qoq" in raw_tool:
        return "compute_growth"
    if "rank" in raw_tool or "top" in raw_tool:
        return "compute_rank"
    if "share" in raw_tool or "ms" in raw_tool:
        return "compute_share"
    if "delta" in raw_tool or "gap" in raw_tool:
        return "compute_delta"
    if "mix" in raw_tool:
        return "compute_mix"
    if "trend" in raw_tool or "series" in raw_tool:
        return "compute_series"
    return raw_tool
