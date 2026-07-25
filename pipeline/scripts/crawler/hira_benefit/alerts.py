from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class FailedRatioEvaluation:
    runs: int
    parsed_count: int
    failed_count: int
    ratio: float
    threshold: float
    triggered: bool


@dataclass(frozen=True, slots=True)
class AlertEvent:
    event: str
    run_id: str
    failed_count: int
    parsed_count: int
    failed_ratio: float
    threshold: float
    window_runs: int


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    status: str
    attempts: int
    reason: str | None = None


def evaluate_failed_ratio(
    runs: Sequence[tuple[int, int]],
    *,
    threshold: float,
) -> FailedRatioEvaluation:
    """Evaluate aggregate FAILED/parsed over a caller-selected recent run window."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    parsed_count = sum(parsed for parsed, _failed in runs)
    failed_count = sum(failed for _parsed, failed in runs)
    ratio = failed_count / parsed_count if parsed_count else 0.0
    return FailedRatioEvaluation(
        runs=len(runs),
        parsed_count=parsed_count,
        failed_count=failed_count,
        ratio=ratio,
        threshold=threshold,
        triggered=bool(parsed_count and ratio >= threshold),
    )


def publish_alert(
    event: AlertEvent,
    *,
    endpoint: str | None,
    attempts: int = 4,
    opener: Callable = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> AlertDelivery:
    """Best-effort alert transport; durable receipts remain the source of truth."""

    if not endpoint:
        return AlertDelivery("disabled", 0, "alert endpoint is not configured")
    attempts = min(max(attempts, 1), 5)
    body = json.dumps(
        asdict(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    last_reason: str | None = None
    for index in range(attempts):
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener(request, 15) as response:
                status = int(getattr(response, "status", 0))
            if 200 <= status < 300:
                return AlertDelivery("published", index + 1)
            last_reason = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort at this boundary.
            last_reason = f"{type(exc).__name__}: {exc}"
        if index + 1 < attempts:
            sleeper(float(2**index))
    return AlertDelivery("failed", attempts, last_reason)
