"""Best-effort completion webhook; delivery failure never changes load success."""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable


SCHEMA_VERSION = "1"
SUPPORTED_EVENTS = frozenset({"complete", "failed", "gate_failed"})
SUPPORTED_SOURCES = frozenset(
    {"ubist", "iqvia_nsa", "iqvia_csd_channel", "iqvia_csd_keyword"}
)
_EVENT_NAMESPACE = uuid.UUID("ef08daf5-54bf-41de-9a32-c90814817845")


def _event_id(*, source: str, epoch: str, manifest_sha: str, run_id: str, event: str) -> str:
    identity = "\x1f".join((SCHEMA_VERSION, source, epoch, manifest_sha, run_id, event))
    return str(uuid.uuid5(_EVENT_NAMESPACE, identity))


@dataclass(frozen=True)
class CompletionSignal:
    event: str
    mode: str
    source: str
    epoch: str
    manifest_sha: str
    run_id: str
    target_schema: str | None
    published_at: str | None
    occurred_at: str
    rows_before: int
    rows_after: int
    rows_loaded: int
    period_from: str | None
    period_to: str | None
    started_at: str
    finished_at: str
    failure_reason: str | None
    log_ref: str
    affected_scope: dict[str, object] | None = None

    def __post_init__(self) -> None:
        required_identity = {
            "run_id": self.run_id,
            "source": self.source,
            "epoch": self.epoch,
            "manifest_sha": self.manifest_sha,
            "occurred_at": self.occurred_at,
        }
        missing = sorted(name for name, value in required_identity.items() if not value)
        if missing:
            raise ValueError(f"missing completion fields: {', '.join(missing)}")
        if self.event not in SUPPORTED_EVENTS:
            raise ValueError(f"unsupported completion event: {self.event!r}")
        if self.source not in SUPPORTED_SOURCES:
            raise ValueError(f"unsupported completion source: {self.source!r}")

    @property
    def category(self) -> str:
        """Internal compatibility alias; outbound v1 carries only ``source``."""
        return self.source

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": _event_id(
                source=self.source,
                epoch=self.epoch,
                manifest_sha=self.manifest_sha,
                run_id=self.run_id,
                event=self.event,
            ),
            "run_id": self.run_id,
            "event": self.event,
            "mode": self.mode,
            "source": self.source,
            "epoch": self.epoch,
            "period": self.epoch,
            "period_range": {"from": self.period_from, "to": self.period_to},
            "target_schema": self.target_schema,
            "published_at": self.published_at,
            "occurred_at": self.occurred_at,
            "manifest_sha": self.manifest_sha,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_loaded": self.rows_loaded,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_reason": self.failure_reason,
            "log_ref": self.log_ref,
            # Retained for additive compatibility with existing consumers.
            "idempotency_key": [self.epoch, self.source, self.manifest_sha],
        }
        if self.affected_scope is not None:
            payload["affected_scope"] = self.affected_scope
        return payload


@dataclass(frozen=True)
class PublishResult:
    status: str
    attempts: int
    reason: str | None = None


def publish(
    signal: CompletionSignal,
    *,
    endpoint: str,
    attempts: int = 4,
    opener: Callable = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> PublishResult:
    if not endpoint.strip():
        return PublishResult("disabled", 0, "completion endpoint is not configured")
    attempts = min(max(int(attempts), 3), 5)
    body = json.dumps(signal.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    last_reason = None
    for index in range(attempts):
        request = urllib.request.Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=15) as response:
                status = int(getattr(response, "status", 0))
            if 200 <= status < 300:
                return PublishResult("published", index + 1)
            last_reason = f"HTTP {status}"
        except Exception as exc:  # delivery is deliberately non-fatal to ingest
            last_reason = f"{type(exc).__name__}: {exc}"
        if index + 1 < attempts:
            sleeper(float(2**index))
    return PublishResult("failed", attempts, last_reason)
