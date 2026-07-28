"""Fail-closed publication contract for deterministic CSD stage refreshes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


RETAIN_MONTHS = 48
DISPLAY_MONTHS = 36
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_KST = timezone(timedelta(hours=9))


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    category: str
    run_id: str
    epoch: str
    incoming_periods: tuple[str, ...]
    builder_commit: str
    image_digest: str
    image_ref: str
    inventory_sha256: str


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    category: str
    run_id: str
    epoch: str
    inventory_sha256: str
    builder_commit: str
    image_digest: str
    window_start: str
    window_end: str
    published_at_utc: str
    published_at_kst: str


class PublicationBackend(Protocol):
    def acquire(self, plan: PublicationPlan) -> None: ...
    def recover(self, plan: PublicationPlan) -> None: ...
    def prepare(self, plan: PublicationPlan) -> None: ...
    def replace_periods(
        self, plan: PublicationPlan, periods: tuple[str, ...]
    ) -> None: ...
    def apply_windows(self, plan: PublicationPlan) -> None: ...
    def arm_recovery(self, plan: PublicationPlan) -> None: ...
    def publish(self, plan: PublicationPlan) -> object: ...
    def record_provenance(
        self, plan: PublicationPlan, record: PublicationRecord
    ) -> None: ...
    def verify_refresh(self, plan: PublicationPlan) -> None: ...
    def complete(self, plan: PublicationPlan) -> None: ...
    def rollback(self, plan: PublicationPlan, token: object) -> None: ...
    def release(self, plan: PublicationPlan) -> None: ...


def validate_plan(plan: PublicationPlan) -> None:
    if plan.category not in {"iqvia_csd_channel", "iqvia_csd_keyword"}:
        raise RuntimeError(
            f"unsupported CSD publication category: {plan.category!r}"
        )
    if not plan.incoming_periods:
        raise RuntimeError("CSD publication requires at least one incoming period")
    if not _COMMIT_RE.fullmatch(plan.builder_commit):
        raise RuntimeError(
            "publication provenance requires a full 40-character builder commit"
        )
    if not _DIGEST_RE.fullmatch(plan.image_digest):
        raise RuntimeError(
            "publication provenance requires a pinned sha256 image digest"
        )
    if not plan.image_ref.endswith(f"@{plan.image_digest}"):
        raise RuntimeError(
            "publication provenance requires an immutable image ref matching the digest"
        )
    if not _SHA_RE.fullmatch(plan.inventory_sha256):
        raise RuntimeError(
            "publication provenance requires a full inventory sha256"
        )


def activate(
    plan: PublicationPlan, backend: PublicationBackend
) -> PublicationRecord:
    """Replace submitted months and publish only after all gates pass."""
    validate_plan(plan)
    periods = tuple(sorted(set(plan.incoming_periods)))
    backend.acquire(plan)
    try:
        backend.recover(plan)
        backend.prepare(plan)
        backend.replace_periods(plan, periods)
        backend.apply_windows(plan)
        backend.arm_recovery(plan)
        token = backend.publish(plan)
        now = datetime.now(timezone.utc)
        record = PublicationRecord(
            category=plan.category,
            run_id=plan.run_id,
            epoch=plan.epoch,
            inventory_sha256=plan.inventory_sha256,
            builder_commit=plan.builder_commit,
            image_digest=plan.image_digest,
            window_start=periods[0],
            window_end=periods[-1],
            published_at_utc=now.isoformat(),
            published_at_kst=now.astimezone(_KST).isoformat(),
        )
        try:
            backend.record_provenance(plan, record)
            backend.verify_refresh(plan)
            backend.complete(plan)
        except Exception:
            backend.rollback(plan, token)
            raise
        return record
    finally:
        backend.release(plan)


def agent_handoff(
    *, category: str, run_id: str, periods: tuple[str, ...]
) -> dict[str, str | bool | None]:
    """Describe the future jw-agent handoff without dispatching it."""
    ordered = tuple(sorted(set(periods)))
    return {
        "enabled": False,
        "category": category,
        "run_id": run_id,
        "period_from": ordered[0] if ordered else None,
        "period_to": ordered[-1] if ordered else None,
        "dispatch": "disabled_separate_jw_agent_round",
    }
