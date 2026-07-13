from __future__ import annotations

import os


WORKFLOW_ID = 316
DEFAULT_WORKFLOW_REV = 5365


def resolve_workflow_rev(explicit: int | None = None) -> int:
    if explicit is not None:
        return explicit
    raw_value = os.environ.get("AGENT3_WORKFLOW_REV")
    if not raw_value:
        return DEFAULT_WORKFLOW_REV
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"AGENT3_WORKFLOW_REV must be an integer, got {raw_value!r}") from exc
