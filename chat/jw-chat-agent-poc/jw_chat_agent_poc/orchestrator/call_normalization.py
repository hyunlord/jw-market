from __future__ import annotations

from typing import Any


def dedupe_blocked_metric_messages(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for call in calls:
        data = call.get("render_data")
        blocked = data.get("blocked_metric_values") if isinstance(data, dict) else None
        if not isinstance(blocked, list):
            normalized.append(call)
            continue
        kept: list[Any] = []
        for item in blocked:
            message = str(item.get("message") or "").strip() if isinstance(item, dict) else ""
            if message and message in seen:
                continue
            if message:
                seen.add(message)
            kept.append(item)
        if len(kept) == len(blocked):
            normalized.append(call)
            continue
        clean_data = dict(data)
        if kept:
            clean_data["blocked_metric_values"] = kept
        else:
            clean_data.pop("blocked_metric_values", None)
        normalized.append({**call, "render_data": clean_data})
    return normalized
