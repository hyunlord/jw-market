from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.common.source_display import source_label_for_tool
from jw_chat_agent_poc.orchestrator.provenance_model import (
    ALL_CHANNELS_LABEL,
    MISSING_LABEL,
    ProvenanceRow,
    dedupe_rows,
    normalized_row,
    period_range,
    period_tokens,
    public_market,
    public_source,
    public_value,
    public_view,
)


def provenance_rows_from_calls(
    calls: Sequence[Mapping[str, Any]],
    sources: Sequence[str],
) -> tuple[ProvenanceRow, ...]:
    rows: list[ProvenanceRow] = []
    for call in calls:
        data = call.get("render_data")
        render_data = data if isinstance(data, Mapping) else {}
        query_spec_value = render_data.get("query_spec")
        query_spec = query_spec_value if isinstance(query_spec_value, Mapping) else {}
        raw_source = _first_value(render_data, call, query_spec, keys=("source_label", "source"))
        tool_source = _source_from_tool(call)
        if tool_source == "지원 범위" or not raw_source or str(raw_source) in _GENERIC_PROVIDER_SOURCES:
            raw_source = tool_source or raw_source
        rows.append(
            normalized_row(
                source=public_source(raw_source),
                period=_period_label(render_data, query_spec),
                view=public_view(
                    _first_value(query_spec, render_data, keys=("view", "view_type")),
                    _first_value(query_spec, render_data, keys=("market", "market_id", "atc4")),
                ),
                market=public_market(
                    _first_value(query_spec, render_data, keys=("market_name", "market_definition")),
                    _first_value(query_spec, render_data, keys=("market", "market_id", "atc4")),
                ),
                denominator=_denominator_label(render_data, query_spec),
                channel=_channel_label(call, render_data, query_spec),
                unit=_unit_label(call, render_data),
            )
        )

    represented_sources = {row.source for row in rows}
    for raw_source in sources:
        if any(
            call.get("tool") == "requested_source_unavailable"
            and str(call.get("source") or "") == raw_source
            for call in calls
        ):
            continue
        if raw_source in _GENERIC_PROVIDER_SOURCES and any(
            str(call.get("source") or "") == raw_source and _source_from_tool(call)
            for call in calls
        ):
            continue
        source = public_source(raw_source)
        if source not in represented_sources:
            rows.append(normalized_row(source=source))
            represented_sources.add(source)
    return dedupe_rows(rows)


def _source_from_tool(call: Mapping[str, Any]) -> str:
    tool = str(call.get("tool") or "")
    if tool == "requested_source_unavailable":
        return "지원 범위"
    mapped = source_label_for_tool(tool)
    if mapped:
        return mapped
    if call.get("safe_url"):
        return "external_api"
    return ""


_GENERIC_PROVIDER_SOURCES = frozenset(
    {"external", "external_api", "nedrug_mcp", "openfda_mcp", "clinicaltrials_mcp"}
)


def _period_label(data: Mapping[str, Any], query_spec: Mapping[str, Any]) -> str:
    direct: list[str] = []
    for container in (query_spec, data):
        for key in ("period", "from_period", "to_period", "requested_period", "fallback_period"):
            value = container.get(key)
            if value not in (None, ""):
                direct.extend(period_tokens(value))
    if not direct:
        direct.extend(_period_values(data))
    return period_range(sorted(set(direct)))


def _period_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"period", "from_period", "to_period"}:
                values.extend(period_tokens(item))
            elif isinstance(item, Mapping | list | tuple):
                values.extend(_period_values(item))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            values.extend(_period_values(item))
    return values


def _channel_label(
    call: Mapping[str, Any],
    data: Mapping[str, Any],
    query_spec: Mapping[str, Any],
) -> str:
    values: list[str] = []
    filter_maps: list[Mapping[str, Any]] = []
    for candidate in (
        call.get("applied_filters"),
        data.get("applied_filters"),
        query_spec.get("applied_filters"),
        query_spec.get("filters"),
    ):
        if isinstance(candidate, Mapping):
            filter_maps.append(candidate)
    for filters in filter_maps:
        for key in ("channel", "visit_location", "specialty", "audit_code"):
            value = filters.get(key)
            if value in (None, "", [], ()):
                continue
            if isinstance(value, Sequence) and not isinstance(value, str | bytes):
                values.extend(str(item).strip() for item in value if str(item).strip())
            else:
                values.append(str(value).strip())
    clean = tuple(dict.fromkeys(public_value(value) for value in values if public_value(value) != MISSING_LABEL))
    return " / ".join(clean) if clean else ALL_CHANNELS_LABEL


def _denominator_label(data: Mapping[str, Any], query_spec: Mapping[str, Any]) -> Any:
    structure = data.get("market_structure")
    if isinstance(structure, Mapping) and str(structure.get("type") or "") == "class_split":
        display_denominator = structure.get("display_denominator")
        if display_denominator not in (None, ""):
            return display_denominator
    return _first_value(
        query_spec,
        data,
        keys=(
            "total_brands_in_market",
            "denominator",
            "rank_denominator",
            "market_brand_count",
            "inherited_denominator",
        ),
    )


def _unit_label(call: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    explicit = _first_value(data, keys=("unit_label", "unit"))
    if explicit:
        return public_value(explicit)
    metric = str(data.get("metric") or data.get("measure") or call.get("tool") or "").lower()
    if any(token in metric for token in ("share", "market_share", "ms_", "top5")):
        return "%"
    if any(token in metric for token in ("sales", "market_size", "revenue")):
        return "억원"
    if "rank" in metric:
        return "위"
    if "patient" in metric or str(call.get("source") or "") == "hira_disease":
        return "명"
    if "activity" in metric or str(call.get("source") or "") == "hira_procedure":
        return "건"
    if any(key in data for key in ("sales_억원", "sales_krw", "market_size_억원", "market_size_recent_krw")):
        return "억원"
    if _contains_nested_key(data, {"value_억원", "value_krw"}):
        return "억원"
    if any(key in data for key in ("ms_recent_pct", "market_share", "ms_delta_pct")):
        return "%"
    return MISSING_LABEL


def _contains_nested_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(key in keys or _contains_nested_key(item, keys) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_contains_nested_key(item, keys) for item in value)
    return False


def _first_value(*containers: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], ()):
                return value
    return ""
