from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Final


_NO_TOOL_RE: Final = re.compile(
    r"(?:이\s*질문에\s*맞(?:는|은)\s*도구가\s*없|"
    r"요청에\s*맞(?:는|은)\s*도구가\s*없)",
    re.IGNORECASE,
)
_ENTITY_NOT_FOUND_RE: Final = re.compile(
    r"(?:요청(?:한)?\s*이름과\s*일치하는\s*브랜드(?:가)?\s*"
    r"(?:없|확인되지)|일치하는\s*브랜드(?:가)?\s*확인되지)",
    re.IGNORECASE,
)
_TOOL_TIMEOUT_RE: Final = re.compile(
    r"(?:조회\s*시간(?:이)?\s*초과|도구\s*조회\s*시간(?:이)?\s*초과|"
    r"\btimeout\b|\btimed\s*out\b)",
    re.IGNORECASE,
)
_SUCCESS_STATUSES: Final = frozenset({"ok", "success", "completed"})
_ERROR_STATUSES: Final = frozenset({"error", "failed", "query_failed"})
_TIMEOUT_STATUSES: Final = frozenset({"timeout", "tool_timeout", "deadline_exceeded"})


def failure_kind(
    answer: str,
    calls: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """Return a stable failure layer only for explicit user-visible or tool signals."""

    if _NO_TOOL_RE.search(answer):
        return "no_tool_planned"
    if _ENTITY_NOT_FOUND_RE.search(answer):
        return "entity_not_found"
    if _TOOL_TIMEOUT_RE.search(answer):
        return "tool_timeout"

    statuses = frozenset(
        status
        for call in calls
        for status in _call_statuses(call)
    )
    if any(status in _SUCCESS_STATUSES for status in statuses):
        return None
    if any(status in _TIMEOUT_STATUSES for status in statuses):
        return "tool_timeout"
    if any(status in _ERROR_STATUSES for status in statuses):
        return "tool_error"
    return None


def _call_statuses(call: Mapping[str, Any]) -> frozenset[str]:
    containers = [call]
    render_data = call.get("render_data")
    if isinstance(render_data, Mapping):
        containers.append(render_data)
    statuses: set[str] = set()
    for container in containers:
        for key in ("status", "tool_status", "source_status", "error_code"):
            value = str(container.get(key) or "").strip().lower()
            if value:
                statuses.add(value)
    return frozenset(statuses)
