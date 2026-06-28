#!/usr/bin/env python3
"""Adaptive event score cutoff helper for deep-analysis event selection."""

from __future__ import annotations

from typing import Any


def adaptive_cutoff(
    events_with_scores: list[dict[str, Any]],
    target_min: int,
    target_max: int,
    init_cutoff: int,
    step: int = 5,
    sort_order: str = "desc",
) -> tuple[list[dict[str, Any]], int]:
    """Filter events by score while keeping the count inside a target band.

    The helper preserves all source scores. It only chooses the API-stage cutoff
    for presentation surfaces such as the events panel and chart markers.
    """
    if target_min < 0:
        raise ValueError("target_min must be >= 0")
    if target_max < target_min:
        raise ValueError("target_max must be >= target_min")
    if step <= 0:
        raise ValueError("step must be > 0")
    if sort_order not in {"desc", "asc"}:
        raise ValueError("sort_order must be 'desc' or 'asc'")

    reverse = sort_order == "desc"
    sorted_events = sorted(
        events_with_scores,
        key=lambda event: int(event.get("score") or 0),
        reverse=reverse,
    )
    if not sorted_events:
        return [], init_cutoff

    cutoff = init_cutoff

    while True:
        count = sum(1 for event in sorted_events if int(event.get("score") or 0) >= cutoff)
        if count <= target_max or cutoff >= 100:
            break
        cutoff += step

    while True:
        count = sum(1 for event in sorted_events if int(event.get("score") or 0) >= cutoff)
        if count >= target_min or cutoff <= 0:
            break
        cutoff -= step

    filtered = [event for event in sorted_events if int(event.get("score") or 0) >= cutoff]
    if len(filtered) > target_max:
        filtered = filtered[:target_max]
    return filtered, cutoff

