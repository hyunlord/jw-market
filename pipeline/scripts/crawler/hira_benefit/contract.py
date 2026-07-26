from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .http_client import HiraRequestPolicy


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    start_to_close: timedelta
    heartbeat_timeout: timedelta
    maximum_attempts: int


ACTIVITY_STAGES: tuple[str, ...] = (
    "discover_changes",
    "collect_details",
    "persist_results",
    "verify_run",
)

ACTIVITY_POLICIES: dict[str, ActivityPolicy] = {
    "discover_changes": ActivityPolicy(timedelta(minutes=2), timedelta(seconds=90), 2),
    "collect_details": ActivityPolicy(timedelta(minutes=30), timedelta(seconds=90), 2),
    "persist_results": ActivityPolicy(timedelta(minutes=5), timedelta(seconds=90), 1),
    "verify_run": ActivityPolicy(timedelta(minutes=5), timedelta(seconds=90), 1),
}


@dataclass(frozen=True, slots=True)
class HiraWorkflowInput:
    run_id: str
    state_root: str
    repo_root: str = "/work"
    index_url: str = (
        "https://www.hira.or.kr/rc/insu/insuadtcrtr/"
        "InsuAdtCrtrList.do"
    )
    base_url: str = "https://www.hira.or.kr"
    first_run_mode: str | None = None
    notice_date_boundary: str | None = None
    manifest_path: str | None = None
    manifest_sha256: str | None = None
    chunk_index: int | None = None
    chunk_size: int = 500
    failed_alert_ratio: float | None = None
    failed_alert_window_runs: int = 3
    request_policy: HiraRequestPolicy = field(default_factory=HiraRequestPolicy)
    workflow_timeout_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id is required")
        if not self.index_url.startswith("https://www.hira.or.kr/"):
            raise ValueError("index_url must use the approved HIRA HTTPS origin")
        if self.first_run_mode not in {None, "backfill_all", "date_boundary"}:
            raise ValueError(
                "first_run_mode must be backfill_all or date_boundary"
            )
        if self.first_run_mode == "date_boundary":
            if self.notice_date_boundary is None:
                raise ValueError(
                    "notice_date_boundary is required for date_boundary"
                )
            try:
                date.fromisoformat(self.notice_date_boundary)
            except ValueError as error:
                raise ValueError(
                    "notice_date_boundary must be an ISO date"
                ) from error
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self.manifest_path is not None:
            if self.manifest_sha256 is None or len(self.manifest_sha256) != 64:
                raise ValueError("manifest_sha256 is required for a backfill chunk")
            if self.chunk_index is None or self.chunk_index < 0:
                raise ValueError("chunk_index is required for a backfill chunk")
        elif self.manifest_sha256 is not None or self.chunk_index is not None:
            raise ValueError("manifest_path is required with chunk identity")
        if self.failed_alert_ratio is not None and not 0.0 <= self.failed_alert_ratio <= 1.0:
            raise ValueError("failed_alert_ratio must be between 0 and 1")
        if self.failed_alert_window_runs <= 0:
            raise ValueError("failed_alert_window_runs must be positive")
        if self.expected_seconds * 3 > self.workflow_timeout_seconds:
            raise ValueError("workflow timeout must retain at least 3x expected margin")

    @property
    def expected_seconds(self) -> int:
        request_seconds = (
            self.request_policy.delay_after_response_seconds
            + 0.20
        )
        return max(60, int(self.chunk_size * request_seconds))


@dataclass(frozen=True, slots=True)
class HiraRunMetrics:
    exit_code: int
    failures: int
    identity_gap: int
    pending_gap: int
    parsed_count: int
    partial_count: int
    failed_count: int


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    failures: tuple[str, ...]
    alerts: tuple[str, ...]


def validate_run_metrics(
    metrics: HiraRunMetrics,
    *,
    failed_alert_ratio: float | None = None,
) -> GateResult:
    failures = tuple(
        field
        for field in ("exit_code", "failures", "identity_gap", "pending_gap")
        if getattr(metrics, field) != 0
    )
    alerts: list[str] = []
    if failed_alert_ratio is not None and metrics.parsed_count > 0:
        ratio = metrics.failed_count / metrics.parsed_count
        if ratio >= failed_alert_ratio:
            alerts.append(
                f"parse_failed_ratio={ratio:.4f} threshold={failed_alert_ratio:.4f}"
            )
    return GateResult(passed=not failures, failures=failures, alerts=tuple(alerts))
