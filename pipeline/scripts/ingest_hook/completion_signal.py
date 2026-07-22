"""Best-effort completion webhook; delivery failure never changes load success."""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class CompletionSignal:
    event: str
    mode: str
    category: str
    epoch: str
    manifest_sha: str
    rows_before: int
    rows_after: int
    rows_loaded: int
    period_from: str | None
    period_to: str | None
    started_at: str
    finished_at: str
    failure_reason: str | None
    log_ref: str

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["idempotency_key"] = [self.epoch, self.category, self.manifest_sha]
        payload["period"] = {"from": payload.pop("period_from"), "to": payload.pop("period_to")}
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
            with opener(request, 15) as response:
                status = int(getattr(response, "status", 0))
            if 200 <= status < 300:
                return PublishResult("published", index + 1)
            last_reason = f"HTTP {status}"
        except Exception as exc:  # delivery is deliberately non-fatal to ingest
            last_reason = f"{type(exc).__name__}: {exc}"
        if index + 1 < attempts:
            sleeper(float(2**index))
    return PublishResult("failed", attempts, last_reason)
