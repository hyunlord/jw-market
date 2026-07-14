from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_RENDER_TYPES = {
    "line": "line",
    "bar": "bar",
    "doughnut": "doughnut",
    "donut": "doughnut",
    "scatter": "scatter",
    "waterfall": "bar",
    "dual_axis_line": "line",
}


def build_bq_chart_specs(payloads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build additive, evidence-backed chart specs from BQ/file chart payloads."""

    charts: list[dict[str, Any]] = []
    for payload in payloads:
        scope = _scope(payload)
        if scope == "MIXED":
            charts.extend(_mixed_specs(payload))
            continue
        if scope is None:
            continue
        chart = _single_spec(payload, forced_scope=None)
        if chart is not None:
            charts.append(chart)
    return charts


def _mixed_specs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for source_key, scope in (("market", "MARKET"), ("file", "FILE")):
        for item in _mapping_items(payload.get(source_key)):
            chart = _single_spec(item, forced_scope=scope)
            if chart is not None:
                charts.append(chart)
    return charts


def _single_spec(payload: Mapping[str, Any], forced_scope: str | None) -> dict[str, Any] | None:
    evidence_refs = _text_items(payload.get("evidence_refs"))
    if not evidence_refs:
        return None
    chart_type = str(payload.get("chart_type") or payload.get("type") or "").strip()
    render_type = _RENDER_TYPES.get(chart_type)
    if render_type is None:
        return None
    datasets = [_copy_json(dataset) for dataset in _mapping_items(payload.get("datasets"))]
    if not datasets:
        return None
    scope = forced_scope or _scope(payload)
    if scope is None:
        return None
    chart: dict[str, Any] = {
        "type": render_type,
        "title": str(payload.get("title") or "BQ chart"),
        "labels": _list_value(payload.get("labels")),
        "datasets": datasets,
        "source": str(payload.get("source") or "BQ evidence"),
        "unit": str(payload.get("unit") or _first_dataset_unit(datasets)),
        "scope": scope,
        "evidence_refs": evidence_refs,
    }
    if chart_type == "waterfall":
        chart["chart_kind"] = "waterfall"
    axes = payload.get("axes")
    if isinstance(axes, Mapping):
        chart["axes"] = _copy_json(axes)
    return chart


def _scope(payload: Mapping[str, Any]) -> str | None:
    value = str(payload.get("scope") or "").strip().upper()
    if value in {"MARKET", "FILE", "MIXED"}:
        return value
    return None


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text_items(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _list_value(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_copy_json(item) for item in value]


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_copy_json(item) for item in value]
    return value


def _first_dataset_unit(datasets: Sequence[Mapping[str, Any]]) -> str:
    first = datasets[0]
    return str(first.get("unit") or "")
