from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import os
from pathlib import Path
import threading
import time
from typing import Final, NewType
from uuid import uuid4

from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue
from pipeline.scripts.etl.brand_activity.brand_activity_replay import (
    DEFAULT_XLSX_GLOB,
    ReplayError,
    ReplayOptions,
    Stage,
    TopicOptions,
    replay,
)
from pipeline.scripts.etl.brand_activity import load_raw_staging

RunId = NewType("RunId", str)
DEFAULT_AUDIT_ROOT_ENV: Final = "BRAND_ACTIVITY_TOPIC_AUDIT_DIR"


class JobStatus(StrEnum):
    """Topic extraction job lifecycle."""

    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TopicRunRequest:
    """Boundary-parsed request for one topic extraction run."""

    execute: bool = False
    save_to_db: bool = False
    max_real_calls: int = 86
    brands_per_market: int | None = None
    brand_rows: int = 5
    axis_per_brand: int = 3
    large_market_limit: int = 0
    token_env: str = "GENOS_BEARER_TOKEN"
    stage_schema: str = "jw_brand_activity_stage"
    raw_schema: str = "jw_brand_activity_raw_stage"
    xlsx: Path = Path(DEFAULT_XLSX_GLOB)


@dataclass(slots=True)  # noqa: MUTABLE_OK
class TopicJob:
    """Mutable in-memory state for one container-local extraction job."""

    run_id: RunId
    status: JobStatus
    request: TopicRunRequest
    started_at: float
    finished_at: float | None = None
    summary: dict[str, JsonValue] | None = None
    error: str = ""


class TopicJobStore:
    """Thread-safe in-memory topic job registry."""

    def __init__(self) -> None:
        self._jobs: dict[RunId, TopicJob] = {}
        self._lock = threading.Lock()

    def start(self, request: TopicRunRequest) -> RunId:
        """Start a topic job in the background and return its id immediately."""
        run_id = RunId(f"topic_{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}")
        job = TopicJob(run_id=run_id, status=JobStatus.RUNNING, request=request, started_at=time.time())
        with self._lock:
            self._jobs[run_id] = job
        thread = threading.Thread(target=self._run, args=(run_id,), name=f"topic-job-{run_id}", daemon=True)
        thread.start()
        return run_id

    def status(self, run_id: RunId) -> dict[str, JsonValue]:
        """Return a JSON-safe status payload."""
        job = self._require(run_id)
        return {
            "run_id": job.run_id,
            "status": job.status.value,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "error": job.error,
        }

    def result(self, run_id: RunId) -> dict[str, JsonValue]:
        """Return a JSON-safe result payload."""
        job = self._require(run_id)
        payload = self.status(run_id)
        payload["request"] = _json_safe_dict(asdict(job.request))
        payload["summary"] = job.summary or {}
        return payload

    def _require(self, run_id: RunId) -> TopicJob:
        with self._lock:
            job = self._jobs.get(run_id)
        if job is None:
            raise TopicJobError(f"unknown run_id: {run_id}")
        return job

    def _run(self, run_id: RunId) -> None:
        job = self._require(run_id)
        try:
            summary = replay(_replay_options(job.request, run_id))
        except (OSError, ReplayError, RuntimeError, ValueError) as exc:
            with self._lock:
                job.status = JobStatus.ERROR
                job.finished_at = time.time()
                job.error = f"{type(exc).__name__}: {exc}"
            return
        with self._lock:
            job.status = JobStatus.DONE
            job.finished_at = time.time()
            job.summary = summary


class TopicJobError(RuntimeError):
    """Raised when a topic job request cannot be fulfilled."""


def parse_topic_request(arguments: dict[str, JsonValue]) -> TopicRunRequest:
    """Parse MCP tool arguments into a typed topic request."""
    dry_run = _bool(arguments.get("dry_run"), default=True)
    execute = _bool(arguments.get("execute"), default=not dry_run)
    return TopicRunRequest(
        execute=execute and not dry_run,
        save_to_db=_bool(arguments.get("save_to_db"), default=False),
        max_real_calls=_int(arguments.get("max_real_calls"), default=86),
        brands_per_market=_optional_int(arguments.get("brands_per_market")),
        brand_rows=_int(arguments.get("brand_rows"), default=5),
        axis_per_brand=_int(arguments.get("axis_per_brand"), default=3),
        large_market_limit=_int(arguments.get("large_market_limit"), default=0),
        token_env=_str(arguments.get("token_env"), default="GENOS_BEARER_TOKEN"),
        stage_schema=_str(arguments.get("stage_schema"), default="jw_brand_activity_stage"),
        raw_schema=_str(arguments.get("raw_schema"), default="jw_brand_activity_raw_stage"),
        xlsx=Path(_str(arguments.get("xlsx"), default=DEFAULT_XLSX_GLOB)),
    )


def _replay_options(request: TopicRunRequest, run_id: RunId) -> ReplayOptions:
    audit_root = Path(os.environ.get(DEFAULT_AUDIT_ROOT_ENV, "audit/brand_activity_topic_server"))
    return ReplayOptions(
        start=Stage.TOPIC,
        only=Stage.TOPIC,
        execute=request.execute,
        save_to_db=request.save_to_db,
        raw_source=load_raw_staging.DEFAULT_SOURCE_ROOT,
        legacy_raw_source=load_raw_staging.DEFAULT_LEGACY_SOURCE_ROOT,
        xlsx=request.xlsx,
        raw_schema=request.raw_schema,
        stage_schema=request.stage_schema,
        window=None,
        audit_dir=audit_root / run_id,
        topic=TopicOptions(
            max_real_calls=request.max_real_calls,
            axis_per_brand=request.axis_per_brand,
            brand_rows=request.brand_rows,
            brands_per_market=request.brands_per_market,
            large_market_limit=request.large_market_limit,
            token_env=request.token_env,
        ),
    )


def _bool(value: JsonValue, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _int(value: JsonValue, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TopicJobError(f"expected integer-compatible value, got {type(value).__name__}")


def _optional_int(value: JsonValue) -> int | None:
    if value is None:
        return None
    return _int(value, default=0)


def _str(value: JsonValue, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    raise TopicJobError(f"expected string value, got {type(value).__name__}")


def _json_safe_dict(values: dict[str, JsonValue | Path]) -> dict[str, JsonValue]:
    return {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}
