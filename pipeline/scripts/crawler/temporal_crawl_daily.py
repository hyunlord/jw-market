"""Temporal orchestration for the durable four-stage JW news crawl chain."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from pipeline.scripts.crawler.crawl_temporal_contract import (
    ACTIVITY_POLICIES,
    ACTIVITY_STAGES,
    INTERNAL_STAGE_BY_ACTIVITY,
    CrawlDailyInput,
    StageGateError,
    activity_command,
    resolve_execution_config,
    write_content_addressed_baseline,
)


TASK_QUEUE = "jw-market-crawl-temporal-shadow-v1"
NAMESPACE = "default"
WORKFLOW_NAME = "jw_agent_crawl_daily_v1"
_TRANSIENT_FAILURE = re.compile(
    r"(?:connection (?:reset|refused|aborted)|timed? ?out|temporary|temporarily|"
    r"deadlock|lock wait timeout|HTTP (?:429|500|502|503|504)|service unavailable)",
    re.IGNORECASE,
)


def _validation_failure(stage: str, code: str, detail: str) -> NoReturn:
    raise ApplicationError(
        f"stage={stage} error_code={code} {detail}",
        type="CrawlDataValidationError",
        non_retryable=True,
    )


def _receipt_path(config: CrawlDailyInput, stage: str) -> Path:
    return Path(config.state_root) / "runs" / config.run_id / "receipts" / f"{stage}.json"


def _load_complete_receipt(config: CrawlDailyInput, stage: str) -> dict[str, Any]:
    path = _receipt_path(config, stage)
    if not path.is_file():
        _validation_failure(stage, "schema_invalid", f"missing receipt: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _validation_failure(stage, "schema_invalid", f"invalid receipt: {exc}")
    if not isinstance(payload, dict) or payload.get("status") != "complete":
        _validation_failure(stage, "schema_invalid", "receipt is not complete")
    for field in ("exit_code", "failures", "events_raw_gap", "pending_gap"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            _validation_failure(stage, "schema_invalid", f"invalid receipt field: {field}")
        if value != 0:
            _validation_failure(stage, field, f"{field}={value}")
    return payload


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _run_stage_process(config: CrawlDailyInput, activity_name: str) -> dict[str, Any]:
    stage = INTERNAL_STAGE_BY_ACTIVITY[activity_name]
    command = activity_command(config, activity_name)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=config.repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    tail: deque[str] = deque(maxlen=200)
    line_count = 0
    assert process.stdout is not None
    try:
        while True:
            try:
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            except TimeoutError:
                activity.heartbeat(
                    {"stage": stage, "state": "running", "lines": line_count}
                )
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            tail.append(line)
            line_count += 1
            activity.logger.info("stage=%s %s", stage, line)
            activity.heartbeat(
                {"stage": stage, "state": "running", "lines": line_count}
            )
        return_code = await process.wait()
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise

    output_tail = list(tail)
    if return_code != 0:
        receipt_path = _receipt_path(config, stage)
        receipt: dict[str, Any] = {}
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                receipt = {}
        error_code = str(receipt.get("error_code") or "stage_nonzero_exit")
        detail = f"return_code={return_code} tail={output_tail[-20:]}"
        if error_code in {
            "reported_failures",
            "events_raw_gap",
            "pending_gap",
            "schema_invalid",
        }:
            _validation_failure(stage, error_code, detail)
        if error_code != "stage_timeout" and not _TRANSIENT_FAILURE.search("\n".join(output_tail)):
            _validation_failure(stage, error_code, detail)
        raise ApplicationError(
            f"stage={stage} error_code=transient_failure {detail}",
            type="CrawlTransientError",
            non_retryable=False,
        )

    receipt = _load_complete_receipt(config, stage)
    return {
        "stage": activity_name,
        "durable_stage": stage,
        "status": "complete",
        "attempt": activity.info().attempt,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "line_count": line_count,
        "output_tail": output_tail[-20:],
        "receipt": receipt,
    }


def _query_baseline_sync() -> Any:
    from pipeline.scripts.crawler.crawl_exposure_baseline import (
        load_eligible_baseline_rows,
    )
    from pipeline.scripts.crawler.tier2_full_scoring_runner import connect_from_env

    conn = connect_from_env()
    try:
        return load_eligible_baseline_rows(conn)
    finally:
        conn.rollback()
        conn.close()


async def _capture_baseline(config: CrawlDailyInput) -> dict[str, Any]:
    from pipeline.scripts.crawler.crawl_exposure_baseline import BaselineOrphanError

    started = time.monotonic()
    activity.heartbeat({"stage": "capture_exposure_baseline", "state": "querying"})
    query = asyncio.create_task(asyncio.to_thread(_query_baseline_sync))
    while not query.done():
        try:
            await asyncio.wait_for(asyncio.shield(query), timeout=30)
        except TimeoutError:
            activity.heartbeat({"stage": "capture_exposure_baseline", "state": "querying"})
    try:
        result = await query
    except BaselineOrphanError as exc:
        _validation_failure("capture_exposure_baseline", "orphan_news", str(exc))
    activity.heartbeat(
        {
            "stage": "capture_exposure_baseline",
            "state": "persisting",
            "pairs": len(result.rows),
        }
    )
    pointer = write_content_addressed_baseline(
        state_root=Path(config.state_root),
        run_id=config.run_id,
        rows=result.rows,
        eligibility_revision=result.eligibility_revision,
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return {
        "stage": "capture_exposure_baseline",
        "status": "complete",
        "attempt": activity.info().attempt,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "exit_code": 0,
        "failures": 0,
        "events_raw_gap": 0,
        "pending_gap": 0,
        **pointer,
    }


async def _run_activity(config: CrawlDailyInput, stage: str) -> dict[str, Any]:
    config = config.validated()
    if config.inject_failure_stage == stage:
        _validation_failure(stage, "injected_failure", "dependency gate injection")
    if config.inject_reported_failures_stage == stage:
        _validation_failure(stage, "reported_failures", "injected failures=1")
    if config.inject_heartbeat_stall_stage == stage:
        timeout = config.test_heartbeat_timeout_seconds or ACTIVITY_POLICIES[stage].heartbeat_seconds
        await asyncio.sleep(timeout + 10)
        _validation_failure(stage, "heartbeat_not_enforced", "stall returned unexpectedly")
    if stage == "capture_exposure_baseline":
        return await _capture_baseline(config)
    try:
        return await _run_stage_process(config, stage)
    except StageGateError as exc:
        _validation_failure(stage, exc.error_code, exc.detail)


@activity.defn(name="capture_exposure_baseline")
async def capture_exposure_baseline(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "capture_exposure_baseline")


@activity.defn(name="tier1_collect")
async def tier1_collect(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "tier1_collect")


@activity.defn(name="tier1_classify")
async def tier1_classify(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "tier1_classify")


@activity.defn(name="tier2_collect")
async def tier2_collect(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "tier2_collect")


@activity.defn(name="tier2_classify_and_refresh")
async def tier2_classify_and_refresh(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "tier2_classify_and_refresh")


ACTIVITY_FUNCTIONS = {
    "capture_exposure_baseline": capture_exposure_baseline,
    "tier1_collect": tier1_collect,
    "tier1_classify": tier1_classify,
    "tier2_collect": tier2_collect,
    "tier2_classify_and_refresh": tier2_classify_and_refresh,
}


@workflow.defn(name=WORKFLOW_NAME)
class CrawlDailyWorkflow:
    @workflow.run
    async def run(self, config: CrawlDailyInput) -> dict[str, Any]:
        config = resolve_execution_config(config, temporal_run_id=workflow.info().run_id)
        stages: list[dict[str, Any]] = []
        for stage in ACTIVITY_STAGES:
            policy = ACTIVITY_POLICIES[stage]
            heartbeat_seconds = config.test_heartbeat_timeout_seconds or policy.heartbeat_seconds
            stages.append(
                await workflow.execute_activity(
                    ACTIVITY_FUNCTIONS[stage],
                    config,
                    start_to_close_timeout=timedelta(seconds=policy.start_to_close_seconds),
                    heartbeat_timeout=timedelta(seconds=heartbeat_seconds),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=30),
                        backoff_coefficient=2.0,
                        maximum_interval=timedelta(minutes=5),
                        maximum_attempts=policy.maximum_attempts,
                    ),
                )
            )
        return {"run_id": config.run_id, "status": "complete", "stages": stages}


ALL_ACTIVITIES = list(ACTIVITY_FUNCTIONS.values())
ALL_WORKFLOWS = [CrawlDailyWorkflow]


def input_as_dict(config: CrawlDailyInput) -> dict[str, Any]:
    return asdict(config.validated())
