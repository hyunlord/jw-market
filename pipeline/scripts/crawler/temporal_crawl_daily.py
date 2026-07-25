"""Temporal orchestration for the durable four-stage JW news crawl chain."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from pipeline.scripts.crawler.crawl_temporal_contract import (
    ACTIVITY_POLICIES,
    INTERNAL_STAGE_BY_ACTIVITY,
    WORKFLOW_ACTIVITY_STAGES,
    CrawlDailyInput,
    StageGateError,
    activity_command,
    resolve_execution_config,
    write_content_addressed_baseline,
)
from pipeline.scripts.crawler.crawl_backlog_policy import (
    read_pending_snapshot,
    write_pending_snapshot,
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
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        pass

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    if process.returncode is None:
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
        start_new_session=True,
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


def _query_pending_baseline_sync() -> Any:
    from pipeline.scripts.crawler.tier2_full_scoring_runner import (
        PENDING_SOURCE_PROCESSOR,
        TIER2_EXACT_PROCESSOR,
        connect_from_env,
        pending_exact_snapshot,
    )

    conn = connect_from_env()
    try:
        return pending_exact_snapshot(
            conn,
            source_processor=TIER2_EXACT_PROCESSOR,
            target_processor=PENDING_SOURCE_PROCESSOR,
        )
    finally:
        conn.rollback()
        conn.close()


async def _capture_baseline(config: CrawlDailyInput) -> dict[str, Any]:
    from pipeline.scripts.crawler.crawl_exposure_baseline import BaselineOrphanError

    started = time.monotonic()
    activity.heartbeat({"stage": "capture_exposure_baseline", "state": "querying"})
    pending_pointer_path = Path(config.state_root) / "runs" / config.run_id / "pending_baseline.json"
    exposure_query = asyncio.create_task(asyncio.to_thread(_query_baseline_sync))
    pending_query = (
        None
        if pending_pointer_path.is_file()
        else asyncio.create_task(asyncio.to_thread(_query_pending_baseline_sync))
    )
    active = {exposure_query}
    if pending_query is not None:
        active.add(pending_query)
    while active:
        _done, active = await asyncio.wait(
            active,
            timeout=30,
            return_when=asyncio.FIRST_COMPLETED,
        )
        activity.heartbeat({"stage": "capture_exposure_baseline", "state": "querying"})
    try:
        result = await exposure_query
        pending_snapshot = (
            read_pending_snapshot(pending_pointer_path)
            if pending_query is None
            else await pending_query
        )
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
    pending_pointer = write_pending_snapshot(
        state_root=Path(config.state_root),
        run_id=config.run_id,
        snapshot=pending_snapshot,
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
        "pending_baseline": pending_pointer,
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
    if stage == "detect_increased_brands":
        return await _detect_increased_brands(config)
    if stage == "agent2_generate":
        return await _agent2_generate(config)
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


async def _detect_increased_brands(config: CrawlDailyInput) -> dict[str, Any]:
    from pipeline.scripts.ai_analysis.agent2_density_worklist import (
        UnknownEventBrandError,
    )
    from pipeline.scripts.crawler.agent2_hook_runtime import (
        detect_and_write_receipt,
    )

    started = time.monotonic()
    activity.heartbeat({"stage": "detect_increased_brands", "state": "querying"})
    try:
        result, pointer = await asyncio.to_thread(
            detect_and_write_receipt,
            config,
        )
    except UnknownEventBrandError as exc:
        _validation_failure("detect_increased_brands", "unresolved_alias", str(exc))
    return {
        "stage": "detect_increased_brands",
        "status": "complete",
        "attempt": activity.info().attempt,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "target_count": result.target_count,
        "targets": [
            {
                "brand_key": target.brand_key,
                "canonical_brand_name": target.canonical_brand_name,
                "effective_added_news_ids": list(target.effective_added_news_ids),
            }
            for target in result.targets
        ],
        **pointer,
    }


def _agent2_call_limit() -> int:
    raw = os.getenv("AGENT2_HOOK_LLM_CALL_LIMIT", "0").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("AGENT2_HOOK_LLM_CALL_LIMIT must be an integer") from exc
    if value < 0:
        raise ValueError("AGENT2_HOOK_LLM_CALL_LIMIT must be non-negative")
    return value


def _agent2_estimated_usd_per_call() -> Decimal:
    from pipeline.scripts.crawler.agent2_hook import (
        DEFAULT_ESTIMATED_USD_PER_CALL,
    )

    raw = os.getenv(
        "AGENT2_HOOK_ESTIMATED_USD_PER_CALL",
        str(DEFAULT_ESTIMATED_USD_PER_CALL),
    ).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(
            "AGENT2_HOOK_ESTIMATED_USD_PER_CALL must be a decimal"
        ) from exc
    if value < 0:
        raise ValueError(
            "AGENT2_HOOK_ESTIMATED_USD_PER_CALL must be non-negative"
        )
    return value


async def _agent2_generate(config: CrawlDailyInput) -> dict[str, Any]:
    from pipeline.scripts.crawler.agent2_hook_receipt import read_detection_receipt
    from pipeline.scripts.crawler.agent2_hook_runtime import (
        Agent2CommandRequest,
        build_agent2_command,
    )

    started = time.monotonic()
    pointer_path = (
        Path(config.state_root)
        / "runs"
        / config.run_id
        / "agent2_detection.json"
    )
    try:
        detection = read_detection_receipt(pointer_path)
        targets = detection["targets"]
        if not isinstance(targets, list):
            raise ValueError("Agent2 detection targets must be a list")
        call_limit = _agent2_call_limit()
        estimated_usd_per_call = _agent2_estimated_usd_per_call()
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        _validation_failure("agent2_generate", "schema_invalid", str(exc))

    target_count = len(targets)
    base_result = {
        "stage": "agent2_generate",
        "status": "complete",
        "attempt": activity.info().attempt,
        "target_count": target_count,
        "expected_llm_calls": target_count,
        "allowed_llm_calls": 0,
        "estimated_usd_per_call": str(estimated_usd_per_call),
        "estimated_cost_usd": str(estimated_usd_per_call * target_count),
        "detection_receipt": str(pointer_path),
    }
    if target_count == 0:
        return {
            **base_result,
            "execution_mode": "no_targets",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    if call_limit == 0:
        return {
            **base_result,
            "execution_mode": "selection_only",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    if target_count > call_limit:
        _validation_failure(
            "agent2_generate",
            "llm_call_limit",
            f"expected_calls={target_count} limit={call_limit}",
        )

    brand_keys = tuple(str(target["brand_key"]) for target in targets)
    command = build_agent2_command(
        Agent2CommandRequest(
            repo_root=Path(config.repo_root),
            state_root=Path(config.state_root),
            content_sha256=str(
                json.loads(pointer_path.read_text(encoding="utf-8"))[
                    "content_sha256"
                ]
            ),
            brand_keys=brand_keys,
            snapshot_at=f"{detection['snapshot_date']}T00:00:00+00:00",
        )
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=config.repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        while process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=30)
            except TimeoutError:
                activity.heartbeat(
                    {
                        "stage": "agent2_generate",
                        "state": "wf217",
                        "target_count": target_count,
                    }
                )
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    if process.returncode != 0:
        output = await process.stdout.read() if process.stdout is not None else b""
        detail = output.decode("utf-8", errors="replace")[-4000:]
        if _TRANSIENT_FAILURE.search(detail):
            raise ApplicationError(
                f"stage=agent2_generate error_code=transient_failure {detail}",
                type="Agent2TransientError",
                non_retryable=False,
            )
        _validation_failure(
            "agent2_generate",
            "wf217_failed",
            f"return_code={process.returncode} tail={detail}",
        )
    generation_dir = (
        Path(config.state_root)
        / "agent2-hook"
        / "generation"
    )
    return {
        **base_result,
        "allowed_llm_calls": target_count,
        "execution_mode": "wf217_enabled",
        "generation_manifest": str(generation_dir / "run_manifest.json"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


@activity.defn(name="detect_increased_brands")
async def detect_increased_brands(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "detect_increased_brands")


@activity.defn(name="agent2_generate")
async def agent2_generate(config: CrawlDailyInput) -> dict[str, Any]:
    return await _run_activity(config, "agent2_generate")


ACTIVITY_FUNCTIONS = {
    "capture_exposure_baseline": capture_exposure_baseline,
    "tier1_collect": tier1_collect,
    "tier1_classify": tier1_classify,
    "tier2_collect": tier2_collect,
    "tier2_classify_and_refresh": tier2_classify_and_refresh,
    "detect_increased_brands": detect_increased_brands,
    "agent2_generate": agent2_generate,
}


@workflow.defn(name=WORKFLOW_NAME)
class CrawlDailyWorkflow:
    @workflow.run
    async def run(self, config: CrawlDailyInput) -> dict[str, Any]:
        config = resolve_execution_config(config, temporal_run_id=workflow.info().run_id)
        stages: list[dict[str, Any]] = []
        for stage in WORKFLOW_ACTIVITY_STAGES:
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
