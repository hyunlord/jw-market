from __future__ import annotations

import os


WORKFLOW_ID = 316


class WorkflowRevNotPinnedError(RuntimeError):
    """Raised when no workflow revision pin is provided.

    Fail-closed by design: a baked-in default revision caused silent stale-rev
    reruns in the past (rev 5365 image vs rev 5692 deployment). The revision
    must always come from the manifest (`AGENT3_WORKFLOW_REV`) or an explicit
    CLI value.
    """


def resolve_workflow_rev(explicit: int | None = None) -> int:
    if explicit is not None:
        return explicit
    raw_value = os.environ.get("AGENT3_WORKFLOW_REV")
    if not raw_value:
        raise WorkflowRevNotPinnedError(
            "AGENT3_WORKFLOW_REV is not set and no --workflow-rev was given; "
            "refusing to fall back to a baked-in default. Pin the revision in "
            "the Job/CronJob manifest env."
        )
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"AGENT3_WORKFLOW_REV must be an integer, got {raw_value!r}") from exc
