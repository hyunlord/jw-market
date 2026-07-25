"""Pure contracts shared by the crawl Temporal worker and its tests."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final
from uuid import UUID


ACTIVITY_STAGES: Final[tuple[str, ...]] = (
    "capture_exposure_baseline",
    "tier1_collect",
    "tier1_classify",
    "tier2_collect",
    "tier2_classify_and_refresh",
)
INTERNAL_STAGE_BY_ACTIVITY: Final[dict[str, str]] = {
    "tier1_collect": "tier1_collect",
    "tier1_classify": "tier1_classify_incremental",
    "tier2_collect": "tier2_collect_exact",
    "tier2_classify_and_refresh": "tier2_classify_v2_and_refresh",
}
CRAWL_STAGES: Final[tuple[str, ...]] = tuple(INTERNAL_STAGE_BY_ACTIVITY.values())
RUN_ID_PATTERN: Final = re.compile(r"^jw-agent-[a-z0-9][a-z0-9._-]{0,95}$")
STAGE_GATE_SCHEMA: Final = "crawl-stage-gate/v1"
BASELINE_SCHEMA: Final = "crawl-exposure-baseline/v1"
WORKFLOW_EXECUTION_TIMEOUT_SECONDS: Final = 86_400


class StageGateError(RuntimeError):
    """A deterministic stage-integrity failure that must not be retried."""

    non_retryable = True

    def __init__(
        self,
        stage: str,
        error_code: str,
        detail: str,
        *,
        gate: StageGate | None = None,
    ) -> None:
        self.stage = stage
        self.error_code = error_code
        self.detail = detail
        self.gate = gate
        super().__init__(f"stage={stage} error_code={error_code} {detail}")


@dataclass(frozen=True, slots=True)
class StageGate:
    stage: str
    exit_code: int
    failures: int
    events_raw_gap: int
    pending_gap: int


@dataclass(frozen=True, slots=True)
class CrawlDailyInput:
    run_id: str
    state_root: str = "/var/lib/jw-crawl-chain"
    stage_script: str = "/work/pipeline/scripts/crawler/crawl_chain_steps.sh"
    repo_root: str = "/work"
    command_revision: str = ""
    use_temporal_run_id: bool = False
    inject_failure_stage: str | None = None
    inject_reported_failures_stage: str | None = None
    inject_heartbeat_stall_stage: str | None = None
    test_heartbeat_timeout_seconds: int | None = None

    def validated(self) -> "CrawlDailyInput":
        if not RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("run_id must start with jw-agent- and contain safe path characters")
        if not self.command_revision:
            raise ValueError("command_revision is required")
        for field_name in (
            "inject_failure_stage",
            "inject_reported_failures_stage",
            "inject_heartbeat_stall_stage",
        ):
            stage = getattr(self, field_name)
            if stage is not None and stage not in ACTIVITY_STAGES:
                raise ValueError(f"{field_name} is not a known activity stage: {stage}")
        if self.test_heartbeat_timeout_seconds is not None and not (
            1 <= self.test_heartbeat_timeout_seconds <= 30
        ):
            raise ValueError("test heartbeat timeout must be between 1 and 30 seconds")
        return self


def resolve_execution_config(
    config: CrawlDailyInput,
    *,
    temporal_run_id: str,
) -> CrawlDailyInput:
    """Resolve one durable run key while preserving fixed shadow identifiers."""

    config = config.validated()
    if not config.use_temporal_run_id:
        return config
    execution_id = str(UUID(temporal_run_id))
    return replace(
        config,
        run_id=f"{config.run_id}-{execution_id}",
        use_temporal_run_id=False,
    ).validated()


@dataclass(frozen=True, slots=True)
class ActivityPolicy:
    start_to_close_seconds: int
    heartbeat_seconds: int
    maximum_attempts: int = 2


ACTIVITY_POLICIES: Final[dict[str, ActivityPolicy]] = {
    "capture_exposure_baseline": ActivityPolicy(900, 120),
    "tier1_collect": ActivityPolicy(10_800, 300, maximum_attempts=1),
    "tier1_classify": ActivityPolicy(1_800, 120),
    "tier2_collect": ActivityPolicy(57_600, 300, maximum_attempts=1),
    "tier2_classify_and_refresh": ActivityPolicy(7_200, 120, maximum_attempts=1),
}


def _integer(payload: dict[str, Any], field: str, stage: str) -> int:
    if field not in payload:
        raise StageGateError(stage, "schema_invalid", f"missing field: {field}")
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageGateError(stage, "schema_invalid", f"field must be an integer: {field}")
    if value < 0:
        raise StageGateError(stage, "schema_invalid", f"field must be non-negative: {field}")
    return value


def read_stage_gate(path: Path, *, expected_stage: str | None = None) -> StageGate:
    """Read and fail-close the four-part activity success contract."""

    if not path.is_file():
        raise StageGateError(expected_stage or "unknown", "schema_invalid", "missing stage_gate.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageGateError(
            expected_stage or "unknown", "schema_invalid", f"invalid stage gate JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise StageGateError(expected_stage or "unknown", "schema_invalid", "stage gate is not an object")
    stage = str(payload.get("stage") or expected_stage or "unknown")
    if payload.get("schema") != STAGE_GATE_SCHEMA:
        raise StageGateError(stage, "schema_invalid", "unexpected stage gate schema")
    if expected_stage is not None and stage != expected_stage:
        raise StageGateError(stage, "schema_invalid", f"expected stage={expected_stage}")
    gate = StageGate(
        stage=stage,
        exit_code=_integer(payload, "exit_code", stage),
        failures=_integer(payload, "failures", stage),
        events_raw_gap=_integer(payload, "events_raw_gap", stage),
        pending_gap=_integer(payload, "pending_gap", stage),
    )
    checks = (
        (gate.exit_code, "nonzero_exit"),
        (gate.failures, "reported_failures"),
        (gate.events_raw_gap, "events_raw_gap"),
        (gate.pending_gap, "pending_gap"),
    )
    for value, error_code in checks:
        if value != 0:
            raise StageGateError(stage, error_code, f"{error_code}={value}", gate=gate)
    return gate


def orchestrator_failure_count(report: object) -> int:
    """Count failed site results in the tier1 orchestrator report."""

    if not isinstance(report, dict):
        raise ValueError("orchestrator report must be an object")
    results = report.get("results")
    if not isinstance(results, list):
        raise ValueError("orchestrator report results must be a list")
    failures = 0
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"orchestrator result {index} must be an object")
        exit_code = result.get("exit_code")
        invalid_exit = isinstance(exit_code, bool) or not isinstance(exit_code, int)
        error = result.get("error")
        invalid_error = error is not None and not isinstance(error, str)
        if invalid_exit or invalid_error or exit_code != 0 or bool(error):
            failures += 1
    return failures


def activity_command(config: CrawlDailyInput, activity_name: str) -> list[str]:
    """Build the fixed one-stage command run by a Temporal activity."""

    config.validated()
    if activity_name not in INTERNAL_STAGE_BY_ACTIVITY:
        raise ValueError(f"not a crawl activity: {activity_name}")
    stage = INTERNAL_STAGE_BY_ACTIVITY[activity_name]
    return [
        "python",
        str(Path(config.repo_root) / "pipeline/scripts/crawler/crawl_chain.py"),
        "run-stage",
        "--run-id",
        config.run_id,
        "--state-root",
        config.state_root,
        "--stage-script",
        config.stage_script,
        "--stage",
        stage,
        "--command-revision",
        config.command_revision,
    ]


async def run_dependency_sequence(
    execute: Callable[[str], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, Any], ...]:
    """Execute in dependency order; exceptions prevent every downstream call."""

    results: list[dict[str, Any]] = []
    for stage in ACTIVITY_STAGES:
        results.append(await execute(stage))
    return tuple(results)


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_content_addressed_baseline(
    *,
    state_root: Path,
    run_id: str,
    rows: Iterable[dict[str, object]],
    eligibility_revision: str,
    captured_at: str,
) -> dict[str, object]:
    """Persist an immutable central-eligibility snapshot and a run pointer."""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("invalid jw-agent run_id")
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        brand = str(row.get("brand_canonical") or "").strip()
        news_id = str(row.get("news_id") or "").strip()
        if not brand or not news_id:
            raise ValueError("baseline row requires brand_canonical and news_id")
        grouped[brand].add(news_id)
    brands = [
        {"brand_canonical": brand, "news_ids": sorted(grouped[brand])}
        for brand in sorted(grouped)
    ]
    snapshot = {
        "schema": BASELINE_SCHEMA,
        "identity": "brand_canonical",
        "eligibility_revision": eligibility_revision,
        "brands": brands,
    }
    encoded = _canonical_json(snapshot)
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    snapshot_path = state_root / "baselines" / f"{content_sha256}.json"
    if snapshot_path.exists():
        if snapshot_path.read_bytes() != encoded:
            raise RuntimeError(f"content-address collision: {snapshot_path}")
    else:
        _atomic_write(snapshot_path, encoded)

    pointer_path = state_root / "runs" / run_id / "baseline.json"
    pointer = {
        "schema": "crawl-exposure-baseline-pointer/v1",
        "run_id": run_id,
        "captured_at": captured_at,
        "eligibility_revision": eligibility_revision,
        "content_sha256": content_sha256,
        "snapshot_path": str(snapshot_path),
        "brand_count": len(brands),
        "pair_count": sum(len(item["news_ids"]) for item in brands),
        "receipt_hit": False,
    }
    if pointer_path.exists():
        saved = json.loads(pointer_path.read_text(encoding="utf-8"))
        if saved.get("content_sha256") != content_sha256:
            raise RuntimeError("baseline changed for an existing run_id")
        return {**saved, "receipt_hit": True}
    _atomic_write(pointer_path, _canonical_json(pointer))
    return pointer
