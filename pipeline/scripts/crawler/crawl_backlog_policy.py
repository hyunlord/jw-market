"""Deterministic pending-backlog gate and SLO policy for the crawl chain."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Iterable


SNAPSHOT_SCHEMA: Final = "crawl-pending-snapshot/v1"
POINTER_SCHEMA: Final = "crawl-pending-snapshot-pointer/v1"
ASSESSMENT_SCHEMA: Final = "crawl-backlog-assessment/v1"
AGE_WARNING_DAYS: Final = 2
AGE_FAILURE_DAYS: Final = 4
TREND_WARNING_RUNS: Final = 2
TREND_FAILURE_RUNS: Final = 4


@dataclass(frozen=True, slots=True, order=True)
class PendingItem:
    news_id: str
    brand_canonical: str
    first_seen_at: datetime

    @property
    def key(self) -> tuple[str, str]:
        return self.news_id, self.brand_canonical


@dataclass(frozen=True, slots=True)
class PendingSnapshot:
    captured_at: datetime
    items: tuple[PendingItem, ...]

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def oldest_pending_at(self) -> datetime | None:
        return min((item.first_seen_at for item in self.items), default=None)

    @property
    def keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(item.key for item in self.items)


@dataclass(frozen=True, slots=True)
class BacklogAssessment:
    before_count: int
    after_count: int
    pending_delta: int
    new_unresolved_count: int
    hard_pass: bool
    oldest_pending_age_days: int | None
    nondecreasing_runs: int
    slo_status: str
    slo_warnings: tuple[str, ...]
    slo_failures: tuple[str, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _nondecreasing_streak(counts: tuple[int, ...]) -> int:
    if not counts or counts[-1] == 0:
        return 0
    streak = 1
    for index in range(len(counts) - 1, 0, -1):
        if counts[index] < counts[index - 1]:
            break
        streak += 1
    return streak


def assess_backlog(
    *,
    before: PendingSnapshot,
    after: PendingSnapshot,
    prior_after_counts: Iterable[int],
) -> BacklogAssessment:
    """Evaluate current-run growth separately from cumulative backlog health."""

    pending_delta = after.count - before.count
    new_unresolved_count = len(after.keys - before.keys)
    oldest = after.oldest_pending_at
    oldest_age_days = (
        max(0, int((_utc(after.captured_at) - _utc(oldest)).total_seconds() // 86_400))
        if oldest is not None
        else None
    )
    counts = tuple(int(value) for value in prior_after_counts) + (after.count,)
    if any(value < 0 for value in counts):
        raise ValueError("pending counts must be non-negative")
    nondecreasing_runs = _nondecreasing_streak(counts)

    warnings: list[str] = []
    failures: list[str] = []
    if oldest_age_days is not None:
        if oldest_age_days >= AGE_FAILURE_DAYS:
            failures.append(f"oldest_pending_age_days>={AGE_FAILURE_DAYS}")
        elif oldest_age_days >= AGE_WARNING_DAYS:
            warnings.append(f"oldest_pending_age_days>={AGE_WARNING_DAYS}")
    if nondecreasing_runs >= TREND_FAILURE_RUNS:
        failures.append(f"nondecreasing_runs>={TREND_FAILURE_RUNS}")
    elif nondecreasing_runs >= TREND_WARNING_RUNS:
        warnings.append(f"nondecreasing_runs>={TREND_WARNING_RUNS}")

    status = "failure" if failures else "warning" if warnings else "ok"
    return BacklogAssessment(
        before_count=before.count,
        after_count=after.count,
        pending_delta=pending_delta,
        new_unresolved_count=new_unresolved_count,
        hard_pass=pending_delta <= 0 and new_unresolved_count == 0,
        oldest_pending_age_days=oldest_age_days,
        nondecreasing_runs=nondecreasing_runs,
        slo_status=status,
        slo_warnings=tuple(warnings),
        slo_failures=tuple(failures),
    )


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _snapshot_payload(snapshot: PendingSnapshot) -> dict[str, object]:
    items = [
        {
            "news_id": item.news_id,
            "brand_canonical": item.brand_canonical,
            "first_seen_at": _utc(item.first_seen_at).isoformat(timespec="seconds"),
        }
        for item in sorted(snapshot.items)
    ]
    return {"schema": SNAPSHOT_SCHEMA, "items": items}


def write_pending_snapshot(
    *,
    state_root: Path,
    run_id: str,
    snapshot: PendingSnapshot,
) -> dict[str, object]:
    """Write immutable pair identities and a run-scoped baseline pointer."""

    encoded = _canonical_json(_snapshot_payload(snapshot))
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    snapshot_path = state_root / "pending-snapshots" / f"{content_sha256}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists() and snapshot_path.read_bytes() != encoded:
        raise RuntimeError(f"content-address collision: {snapshot_path}")
    if not snapshot_path.exists():
        temporary = snapshot_path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(snapshot_path)

    pointer = {
        "schema": POINTER_SCHEMA,
        "run_id": run_id,
        "captured_at": _utc(snapshot.captured_at).isoformat(timespec="seconds"),
        "content_sha256": content_sha256,
        "snapshot_path": str(snapshot_path),
        "pending_count": snapshot.count,
        "oldest_pending_at": (
            _utc(snapshot.oldest_pending_at).isoformat(timespec="seconds")
            if snapshot.oldest_pending_at is not None
            else None
        ),
    }
    pointer_path = state_root / "runs" / run_id / "pending_baseline.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    if pointer_path.exists():
        saved = json.loads(pointer_path.read_text(encoding="utf-8"))
        if saved.get("content_sha256") != content_sha256:
            raise RuntimeError("pending baseline changed for an existing run_id")
        return {**saved, "receipt_hit": True}
    temporary = pointer_path.with_suffix(".tmp")
    temporary.write_bytes(_canonical_json(pointer))
    temporary.replace(pointer_path)
    return {**pointer, "receipt_hit": False}


def read_pending_snapshot(pointer_path: Path) -> PendingSnapshot:
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if pointer.get("schema") != POINTER_SCHEMA:
        raise ValueError("unexpected pending pointer schema")
    snapshot_path = Path(str(pointer["snapshot_path"]))
    encoded = snapshot_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != pointer.get("content_sha256"):
        raise ValueError("pending snapshot hash mismatch")
    payload = json.loads(encoded)
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unexpected pending snapshot schema")
    items = tuple(
        PendingItem(
            news_id=str(row["news_id"]),
            brand_canonical=str(row["brand_canonical"]),
            first_seen_at=datetime.fromisoformat(str(row["first_seen_at"])),
        )
        for row in payload["items"]
    )
    return PendingSnapshot(
        captured_at=datetime.fromisoformat(str(pointer["captured_at"])),
        items=items,
    )


def assessment_payload(
    assessment: BacklogAssessment,
    *,
    run_id: str,
    captured_at: datetime,
) -> dict[str, object]:
    return {
        "schema": ASSESSMENT_SCHEMA,
        "run_id": run_id,
        "captured_at": _utc(captured_at).isoformat(timespec="seconds"),
        **asdict(assessment),
    }
