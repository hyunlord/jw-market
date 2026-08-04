from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jw_chat_agent_poc.tool_use.v3_file_projection import file_source_values


ProjectionCandidate = tuple[Mapping[str, object], tuple[str, ...], str]


@dataclass(frozen=True, slots=True)
class CanonicalProjection:
    values: Mapping[str, object | None]
    sources: tuple[tuple[str, str], ...]
    missing_reasons: tuple[tuple[str, str], ...]

    def missing(self, required: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(field for field in required if not _is_present(self.values[field]))

    def reasons(self, required: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        missing = set(self.missing(required))
        return tuple(item for item in self.missing_reasons if item[0] in missing)


def project_market_fact(
    tool_name: str,
    arguments: Mapping[str, object],
    raw: object,
) -> CanonicalProjection:
    result = _mapping(raw)
    render_data = _mapping(_read_path(result, ("render_data",)))
    schema_values = _market_schema_values(tool_name, render_data)
    candidates = {
        "entity": (
            (result, ("render_data", "brand"), "raw.render_data.brand"),
            (result, ("render_data", "anchor_brand"), "raw.render_data.anchor_brand"),
            (
                result,
                ("render_data", "query_spec", "filters", "brand"),
                "raw.render_data.query_spec.filters.brand",
            ),
            (result, ("brand",), "raw.brand"),
            (result, ("anchor_brand",), "raw.anchor_brand"),
            (arguments, ("brand",), "arguments.brand"),
        ),
        "metric": (
            (schema_values, ("metric",), schema_values.get("metric_source", "schema.metric")),
            (
                result,
                ("render_data", "query_spec", "metrics"),
                "raw.render_data.query_spec.metrics",
            ),
            (result, ("render_data", "metric"), "raw.render_data.metric"),
            (result, ("render_data", "measure"), "raw.render_data.measure"),
            (result, ("metric",), "raw.metric"),
            (result, ("measure",), "raw.measure"),
            (arguments, ("metric",), "arguments.metric"),
        ),
        "period": (
            (result, ("render_data", "period"), "raw.render_data.period"),
            (
                result,
                ("render_data", "query_spec", "filters", "period"),
                "raw.render_data.query_spec.filters.period",
            ),
            (result, ("period",), "raw.period"),
            (arguments, ("period",), "arguments.period"),
        ),
        "unit": (
            (schema_values, ("unit",), schema_values.get("unit_source", "schema.unit")),
            (result, ("render_data", "unit_label"), "raw.render_data.unit_label"),
            (result, ("render_data", "unit"), "raw.render_data.unit"),
            (result, ("unit_label",), "raw.unit_label"),
            (result, ("unit",), "raw.unit"),
            (arguments, ("unit",), "arguments.unit"),
        ),
        "view": (
            (result, ("render_data", "view_type"), "raw.render_data.view_type"),
            (
                result,
                ("render_data", "query_spec", "view"),
                "raw.render_data.query_spec.view",
            ),
            (result, ("view_type",), "raw.view_type"),
            (result, ("query_spec", "view"), "raw.query_spec.view"),
            (arguments, ("view",), "arguments.view"),
        ),
        "market": (
            (result, ("render_data", "market_id"), "raw.render_data.market_id"),
            (
                result,
                ("render_data", "query_spec", "market"),
                "raw.render_data.query_spec.market",
            ),
            (result, ("market_id",), "raw.market_id"),
            (result, ("query_spec", "market"), "raw.query_spec.market"),
            (result, ("render_data", "market"), "raw.render_data.market"),
            (arguments, ("market",), "arguments.market"),
        ),
    }
    return _project(candidates, reject_period_placeholders=True)


def project_regulatory_fact(raw: object) -> CanonicalProjection:
    result = _mapping(raw)
    return _project(
        {
            "effective_date": _result_candidates(result, "effective_date"),
            "last_checked": _result_candidates(result, "last_checked"),
        }
    )


def project_clinical_fact(raw: object) -> CanonicalProjection:
    result = _mapping(raw)
    return _project(
        {
            "status": (
                *_result_candidates(result, "status"),
                *_result_candidates(result, "overallStatus"),
            ),
            "last_update_posted": (
                *_result_candidates(result, "last_update_posted"),
                *_result_candidates(result, "lastUpdatePostDate"),
            ),
        }
    )


def project_file_fact(
    arguments: Mapping[str, object],
    raw: object,
) -> CanonicalProjection:
    result = _mapping(raw)
    source_values = file_source_values(arguments, raw)
    return _project(
        {
            field: (
                *_result_candidates(result, field),
                (arguments, (field,), f"arguments.{field}"),
                (
                    source_values,
                    (field,),
                    source_values.get(f"{field}_source", f"sources.{field}"),
                ),
            )
            for field in ("file_id", "sheet", "range")
        }
    )


def _result_candidates(
    result: Mapping[str, object],
    field: str,
) -> tuple[tuple[Mapping[str, object], tuple[str, ...], str], ...]:
    return (
        (result, (field,), f"raw.{field}"),
        (result, ("render_data", field), f"raw.render_data.{field}"),
        (result, ("detail", field), f"raw.detail.{field}"),
        (result, ("render_data", "detail", field), f"raw.render_data.detail.{field}"),
    )


def _project(
    candidates: Mapping[str, Sequence[ProjectionCandidate]],
    *,
    reject_period_placeholders: bool = False,
) -> CanonicalProjection:
    values: dict[str, object | None] = {}
    sources: list[tuple[str, str]] = []
    missing_reasons: list[tuple[str, str]] = []
    for field, field_candidates in candidates.items():
        values[field] = None
        rejection_reason: str | None = None
        for root, path, source in field_candidates:
            value, candidate_reason = _single_value(_read_path(root, path))
            if candidate_reason is not None:
                rejection_reason = candidate_reason
                continue
            if reject_period_placeholders and field == "period" and _is_period_placeholder(value):
                rejection_reason = "placeholder_not_canonical"
                continue
            if field == "metric" and _is_metric_sentinel(value):
                rejection_reason = "sentinel_not_canonical"
                continue
            if _is_present(value):
                values[field] = value
                sources.append((field, source))
                break
        if not _is_present(values[field]):
            missing_reasons.append(
                (field, rejection_reason or "not_present_in_explicit_sources")
            )
    return CanonicalProjection(
        values=values,
        sources=tuple(sources),
        missing_reasons=tuple(missing_reasons),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _read_path(root: Mapping[str, object], path: tuple[str, ...]) -> object | None:
    current: object = root
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _single_value(value: object) -> tuple[object | None, str | None]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 1:
            return value[0], None
        if len(value) > 1:
            return None, "multiple_values_not_canonical"
    return value, None


def _market_schema_values(
    tool_name: str,
    render_data: Mapping[str, object],
) -> Mapping[str, object]:
    if tool_name == "market.get_market_size" and _is_present(
        render_data.get("market_size_recent_krw")
    ):
        return {
            "metric": "market_size",
            "metric_source": "raw.render_data.market_size_recent_krw[field]",
            "unit": "KRW",
            "unit_source": "raw.render_data.market_size_recent_krw[field]",
        }
    if tool_name == "market.get_market_members" and _is_present(
        render_data.get("member_population")
    ):
        return {
            "metric": "market_members",
            "metric_source": "raw.render_data.member_population[field]",
        }
    return {}


def _is_period_placeholder(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in {
        "latest",
        "current",
        "recent",
        "최신",
        "최근",
    }


def _is_metric_sentinel(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == "query_spec"


def _is_present(value: object) -> bool:
    return value is not None and value != "" and value != () and value != []
