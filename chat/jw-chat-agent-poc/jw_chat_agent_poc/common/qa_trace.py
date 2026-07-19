from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def qa_trace_started_at() -> datetime:
    return datetime.now(UTC)


def attach_tool_qa_trace(
    call: dict[str, Any],
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    status: str | None = None,
    row_count: int | None = None,
    data_as_of: str | None = None,
    cache_hit: bool | None = None,
) -> dict[str, Any]:
    resolved_status = status or tool_status(call)
    backend_trace = call.get("backend_trace")
    backend_items = backend_trace if isinstance(backend_trace, Mapping) else {}
    trace = {
        "started_at": started_at.isoformat(),
        "ended_at": (ended_at or datetime.now(UTC)).isoformat(),
        "status": resolved_status,
        "row_count": tool_row_count(call, status=resolved_status) if row_count is None else row_count,
        "data_as_of": tool_data_as_of(call) if data_as_of is None else data_as_of,
        "cache_hit": tool_cache_hit(call) if cache_hit is None else cache_hit,
    }
    for key in ("endpoint", "latency_ms", "source_epoch", "built_at"):
        if key in backend_items:
            trace[key] = backend_items.get(key)
    call["qa_trace"] = trace
    return call


def tool_status(call: Mapping[str, Any]) -> str:
    render_data = call.get("render_data")
    nested = str(render_data.get("status") or "").strip() if isinstance(render_data, Mapping) else ""
    top_level = str(call.get("status") or "").strip()
    if nested or top_level:
        return nested or top_level
    if isinstance(render_data, Mapping):
        if render_data.get("unavailable") or render_data.get("data_absent"):
            return "no_data"
        if render_data.get("error"):
            return "error"
        return "ok"
    return "unknown"


def tool_row_count(call: Mapping[str, Any], *, status: str | None = None) -> int:
    render_data = call.get("render_data")
    containers = (call, render_data) if isinstance(render_data, Mapping) else (call,)
    for container in containers:
        for key in ("row_count", "rows_count", "total_count", "count"):
            value = container.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
    if isinstance(render_data, Mapping):
        for key in ("rows", "items", "series", "brand_value_series_10pt", "evidence"):
            value = render_data.get(key)
            if isinstance(value, (list, tuple)):
                return len(value)
    resolved_status = (status or tool_status(call)).strip().lower()
    return 1 if resolved_status == "ok" and isinstance(render_data, Mapping) and bool(render_data) else 0


def tool_data_as_of(call: Mapping[str, Any]) -> str | None:
    render_data = call.get("render_data")
    containers = (call, render_data) if isinstance(render_data, Mapping) else (call,)
    for container in containers:
        for key in ("data_as_of", "period_recent", "period"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return None


def tool_cache_hit(call: Mapping[str, Any]) -> bool:
    render_data = call.get("render_data")
    backend_trace = call.get("backend_trace")
    return bool(
        call.get("cache_hit")
        or (render_data.get("cache_hit") if isinstance(render_data, Mapping) else False)
        or (backend_trace.get("cache_hit") if isinstance(backend_trace, Mapping) else False)
    )
