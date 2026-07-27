from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from .http_client import LIST_SLOW_RESPONSE_SECONDS, HiraRequestPolicy


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    start_to_close: timedelta
    heartbeat_timeout: timedelta
    maximum_attempts: int


#: Index enumeration is split so a single activity never owns every list page.
#: ``discover_probe`` fetches page 1 (and persists its page receipt), the page
#: batches fetch pages 2..N, and ``discover_reduce`` verifies the complete page
#: set before any comparison against stored state happens.
DISCOVERY_ENUMERATION_STAGES: tuple[str, ...] = (
    "discover_probe",
    "discover_page_batch",
)
POST_DISCOVERY_STAGES: tuple[str, ...] = (
    "collect_details",
    "persist_results",
    "verify_run",
)
ACTIVITY_STAGES: tuple[str, ...] = (
    *DISCOVERY_ENUMERATION_STAGES,
    "discover_reduce",
    *POST_DISCOVERY_STAGES,
)

ACTIVITY_POLICIES: dict[str, ActivityPolicy] = {
    "discover_probe": ActivityPolicy(timedelta(minutes=1), timedelta(seconds=90), 2),
    "discover_page_batch": ActivityPolicy(
        timedelta(minutes=5),
        timedelta(seconds=90),
        2,
    ),
    "discover_reduce": ActivityPolicy(timedelta(minutes=5), timedelta(seconds=90), 2),
    "collect_details": ActivityPolicy(timedelta(minutes=30), timedelta(seconds=90), 2),
    "persist_results": ActivityPolicy(timedelta(minutes=5), timedelta(seconds=90), 1),
    "verify_run": ActivityPolicy(timedelta(minutes=5), timedelta(seconds=90), 1),
}

#: Subprocess spawn plus interpreter/import cost charged to every stage.
STAGE_STARTUP_SECONDS = 10.0
#: ``discover_reduce`` performs no HTTP: receipt reads, a state load and a compare.
REDUCE_EXPECTED_SECONDS = 30.0
#: Detail responses are an order of magnitude faster than list responses; the
#: 0.20s coefficient is the pre-existing, unchanged detail-response allowance.
DETAIL_RESPONSE_SECONDS = 0.20
#: Pages fetched per ``discover_page_batch`` attempt. Derived, not guessed:
#: ``discover_page_batch`` owns a 300s StartToClose, the house rule keeps a 3x
#: margin, so one attempt may plan for 100s of work. Subtracting the 10s stage
#: startup leaves 90s, and the worst paced page inside the circuit breaker costs
#: ``2.0 pacing + 0.5 jitter + 2.212 slow-response`` = 4.712s, i.e. 19.1 pages.
#: 18 is that ceiling with one page of slack.
PAGES_PER_BATCH = 18


def stage_receipt_name(
    stage: str,
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> str:
    """Receipt identity for a stage attempt.

    Page batches need one receipt each; sharing ``discover_page_batch`` would let
    the first completed batch mark every later batch resumable.
    """

    if stage != "discover_page_batch":
        return stage
    if page_start is None or page_end is None:
        raise ValueError("discover_page_batch requires a page range")
    return f"discover_page_batch.p{page_start:04d}-{page_end:04d}"


def page_batches(
    total_pages: int,
    batch_size: int,
    *,
    first_page: int = 2,
) -> tuple[tuple[int, int], ...]:
    """Split pages ``first_page..total_pages`` into inclusive batch ranges.

    ``discover_probe`` already persisted page 1, so batching starts at page 2.
    Page growth adds batches; it never enlarges an individual batch budget.
    """

    if total_pages < 1:
        raise ValueError("total_pages must be positive")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    return tuple(
        (start, min(start + batch_size - 1, total_pages))
        for start in range(first_page, total_pages + 1, batch_size)
    )


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
    #: Declared list-page count used by the budget gate. Enumeration cost was
    #: invisible to the gate before this field existed, which is how a 153-page
    #: enumeration was admitted against a 120s activity budget.
    expected_index_pages: int = 160
    #: Declared detail-fetch volume. ``None`` falls back to ``chunk_size`` so
    #: backfill chunk configs keep their existing budget exactly.
    expected_detail_notices: int | None = None
    pages_per_batch: int = PAGES_PER_BATCH
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
        if self.expected_index_pages <= 0:
            raise ValueError("expected_index_pages must be positive")
        if self.pages_per_batch <= 0:
            raise ValueError("pages_per_batch must be positive")
        declared_details = self.expected_detail_notices
        if declared_details is not None and declared_details < 0:
            raise ValueError("expected_detail_notices cannot be negative")
        batch_budget = ACTIVITY_POLICIES["discover_page_batch"].start_to_close
        if self.page_batch_worst_seconds * 3 > batch_budget.total_seconds():
            raise ValueError(
                "page batch budget must retain at least 3x margin over paced enumeration"
            )
        if self.expected_seconds * 3 > self.workflow_timeout_seconds:
            raise ValueError("workflow timeout must retain at least 3x expected margin")

    @property
    def detail_request_seconds(self) -> float:
        return (
            self.request_policy.delay_after_response_seconds
            + DETAIL_RESPONSE_SECONDS
        )

    @property
    def list_request_seconds(self) -> float:
        """Mean paced cost of one list page: pacing + mean jitter + response."""

        return (
            self.request_policy.delay_after_response_seconds
            + self.request_policy.request_jitter_seconds / 2
            + LIST_SLOW_RESPONSE_SECONDS
        )

    @property
    def list_request_worst_seconds(self) -> float:
        """Worst paced cost of one list page still inside the circuit breaker."""

        return (
            self.request_policy.delay_after_response_seconds
            + self.request_policy.request_jitter_seconds
            + LIST_SLOW_RESPONSE_SECONDS
        )

    @property
    def page_batch_worst_seconds(self) -> float:
        return (
            self.pages_per_batch * self.list_request_worst_seconds
            + STAGE_STARTUP_SECONDS
        )

    @property
    def enumeration_page_count(self) -> int:
        """Pages the run actually enumerates; a backfill chunk enumerates none."""

        return 0 if self.manifest_path is not None else self.expected_index_pages

    @property
    def enumeration_batch_count(self) -> int:
        pages = self.enumeration_page_count
        if pages == 0:
            return 0
        return len(page_batches(pages, self.pages_per_batch))

    @property
    def expected_detail_count(self) -> int:
        if self.expected_detail_notices is None:
            return self.chunk_size
        return self.expected_detail_notices

    @property
    def discovery_expected_seconds(self) -> float:
        """Probe + page batches + reduce.

        A backfill chunk reads its manifest instead of enumerating, so it only
        pays the reduce stage.
        """

        pages = self.enumeration_page_count
        if pages == 0:
            return REDUCE_EXPECTED_SECONDS + STAGE_STARTUP_SECONDS
        stages = self.enumeration_batch_count + 2  # probe + batches + reduce
        return (
            pages * self.list_request_seconds
            + stages * STAGE_STARTUP_SECONDS
            + REDUCE_EXPECTED_SECONDS
        )

    @property
    def detail_expected_seconds(self) -> float:
        """collect_details + persist_results + verify_run."""

        return (
            self.expected_detail_count * self.detail_request_seconds
            + 3 * STAGE_STARTUP_SECONDS
        )

    @property
    def expected_seconds(self) -> int:
        return max(
            60,
            math.ceil(self.discovery_expected_seconds + self.detail_expected_seconds),
        )


SCHEDULE_RUN_ID = "temporal-scheduled"
#: A daily incremental run enumerates the whole index but fetches details only
#: for new and changed notices. ``chunk_size`` (500) is a backfill ceiling, and
#: budgeting the daily run against it left no room for enumeration at all.
#: 120 sits ~14% under the largest value the 3x gate admits alongside a 160-page
#: enumeration. A delta larger than this belongs on the backfill manifest path,
#: which skips enumeration entirely.
SCHEDULED_DETAIL_NOTICES = 120


def scheduled_workflow_input(
    *,
    state_root: str,
    notice_date_boundary: str,
    run_id: str = SCHEDULE_RUN_ID,
) -> HiraWorkflowInput:
    """Build incremental input with a fail-closed date-boundary fallback.

    Lives here, not in ``schedule.py``, so the production budget is assertable
    without the Temporal SDK installed.
    """

    return HiraWorkflowInput(
        run_id=run_id,
        state_root=state_root,
        first_run_mode="date_boundary",
        notice_date_boundary=notice_date_boundary,
        expected_detail_notices=SCHEDULED_DETAIL_NOTICES,
    )


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
