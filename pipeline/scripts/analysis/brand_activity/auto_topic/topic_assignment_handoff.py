from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Final

from .models import JsonValue
from .row_topic_assignment import AssignmentInputRow


AXIS_COMPLETE: Final = "complete"
AXIS_INCOMPLETE: Final = "incomplete"
ASSIGNMENT_BLOCKED: Final = "blocked"
ASSIGNMENT_PENDING: Final = "pending"
ASSIGNMENT_RUNNING: Final = "running"
ASSIGNMENT_COMPLETE: Final = "complete"
ASSIGNMENT_GAP: Final = "gap"
RECONCILABLE_ASSIGNMENT_STATUSES: Final = (
    ASSIGNMENT_PENDING,
    ASSIGNMENT_RUNNING,
    ASSIGNMENT_GAP,
)


class HandoffBlockedError(RuntimeError):
    """Raised when row assignment lacks an exact completed-axis receipt."""


@dataclass(frozen=True, slots=True)
class TopicScopeSnapshot:
    """Canonical fields that identify one stored topic scope."""

    scope_id: str
    display_name: str
    atc4_values: tuple[str, ...]
    quality_grade: str
    source_row_count: int
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Identity:
    """Count and content hash for an exact durable population."""

    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AxisCompletion:
    """Fail-closed result of comparing expected and stored topic scopes."""

    axis_status: str
    assignment_status: str
    expected_scope_count: int
    stored_scope_count: int
    whole_run_eligible: bool


@dataclass(frozen=True, slots=True)
class AssignmentHandoffReceipt:
    """Durable handoff contract between topic generation and row assignment."""

    run_id: str
    target_mode: str
    input_fingerprint: str
    expected_scope_count: int
    stored_scope_count: int
    scope_identity_sha256: str
    assignment_population_count: int
    assignment_population_sha256: str
    axis_status: str
    assignment_status: str


@dataclass(frozen=True, slots=True)
class AssignmentStatusSnapshot:
    """Stored row-level evidence used by reconciliation."""

    scope_id: str
    row_id: int
    stage_row_sha256: str
    assignment_count: int


@dataclass(frozen=True, slots=True)
class AssignmentGap:
    """Exact reconciliation result for one topic-set version."""

    complete: bool
    expected_row_count: int
    status_row_count: int
    missing_row_ids: tuple[int, ...]
    hash_mismatch_row_ids: tuple[int, ...]
    zero_assignment_scope_ids: tuple[str, ...]


def scope_identity(scopes: Sequence[TopicScopeSnapshot]) -> Identity:
    """Hash canonical stored topic fields without depending on row order."""
    lines = [
        json.dumps(
            {
                "scope_id": scope.scope_id,
                "display_name": scope.display_name,
                "atc4_values": sorted(scope.atc4_values),
                "quality_grade": scope.quality_grade,
                "source_row_count": scope.source_row_count,
                "payload": scope.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for scope in scopes
    ]
    return _identity(lines)


def population_identity(rows: Sequence[AssignmentInputRow]) -> Identity:
    """Hash exact scope, row id, and stage hash tuples without row-order drift."""
    lines = [
        json.dumps(
            [row.scope_id, row.row_id, row.stage_row_sha256],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in rows
    ]
    return _identity(lines)


def evaluate_axis_completion(expected: Identity, stored: Identity) -> AxisCompletion:
    """Allow the whole run only when scope count and content hash are exact."""
    complete = expected == stored and expected.count > 0
    return AxisCompletion(
        axis_status=AXIS_COMPLETE if complete else AXIS_INCOMPLETE,
        assignment_status=ASSIGNMENT_PENDING if complete else ASSIGNMENT_BLOCKED,
        expected_scope_count=expected.count,
        stored_scope_count=stored.count,
        whole_run_eligible=complete,
    )


def require_assignment_ready(
    receipt: AssignmentHandoffReceipt | None,
    rows: Sequence[AssignmentInputRow],
) -> None:
    """Reject absent, incomplete, or population-drifted handoffs."""
    if receipt is None:
        raise HandoffBlockedError("assignment handoff receipt is missing")
    if receipt.axis_status != AXIS_COMPLETE:
        raise HandoffBlockedError(
            f"assignment blocked by axis_status={receipt.axis_status}"
        )
    current = population_identity(rows)
    if (
        current.count != receipt.assignment_population_count
        or current.sha256 != receipt.assignment_population_sha256
    ):
        raise HandoffBlockedError(
            "assignment population differs from completed-axis receipt"
        )


def reconcilable_run_ids(
    receipts: Sequence[AssignmentHandoffReceipt],
) -> tuple[str, ...]:
    """Return exact pending run ids; completed or blocked runs are excluded."""
    return tuple(
        receipt.run_id
        for receipt in receipts
        if receipt.axis_status == AXIS_COMPLETE
        and receipt.assignment_status in RECONCILABLE_ASSIGNMENT_STATUSES
    )


def evaluate_assignment_gap(
    expected_rows: Sequence[AssignmentInputRow],
    status_rows: Sequence[AssignmentStatusSnapshot],
    *,
    assignment_scope_counts: Mapping[str, int],
) -> AssignmentGap:
    """Compare exact row/hash identity and flag scopes with no assignments."""
    status_by_key = {
        (status.scope_id, status.row_id): status
        for status in status_rows
    }
    missing: list[int] = []
    mismatched: list[int] = []
    scopes_with_rows = {row.scope_id for row in expected_rows}
    for row in expected_rows:
        status = status_by_key.get((row.scope_id, row.row_id))
        if status is None:
            missing.append(row.row_id)
            continue
        if status.stage_row_sha256 != row.stage_row_sha256:
            missing.append(row.row_id)
            mismatched.append(row.row_id)
    zero_scopes = sorted(
        scope_id
        for scope_id in scopes_with_rows
        if assignment_scope_counts.get(scope_id, 0) == 0
    )
    return AssignmentGap(
        complete=not missing and not zero_scopes,
        expected_row_count=len(expected_rows),
        status_row_count=len(status_rows),
        missing_row_ids=tuple(sorted(set(missing))),
        hash_mismatch_row_ids=tuple(sorted(set(mismatched))),
        zero_assignment_scope_ids=tuple(zero_scopes),
    )


def _identity(lines: Sequence[str]) -> Identity:
    digest = hashlib.sha256()
    for line in sorted(lines):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return Identity(count=len(lines), sha256=digest.hexdigest())
