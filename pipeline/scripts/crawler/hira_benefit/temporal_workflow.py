"""Temporal workflow for HIRA benefit criteria.

This module defines a worker-loadable workflow only. It deliberately contains
no schedule creation or workflow-start command.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .contract import (
        ACTIVITY_POLICIES,
        ACTIVITY_STAGES,
        POST_DISCOVERY_STAGES,
        SCHEDULE_RUN_ID,
        HiraWorkflowInput,
        page_batches,
        stage_receipt_name,
    )
    from .receipts import read_json, run_dir, write_json
    from .runtime import run_subprocess_with_heartbeat, stage_log_path


TASK_QUEUE = "jw-market-hira-benefit-v1"
WORKFLOW_NAME = "jw_hira_benefit_daily_v1"


@dataclass(frozen=True, slots=True)
class HiraStageRequest:
    config: HiraWorkflowInput
    stage: str
    page_start: int | None = None
    page_end: int | None = None


def resolve_run_config(
    config: HiraWorkflowInput,
    *,
    workflow_run_id: str,
) -> HiraWorkflowInput:
    """Give each scheduled execution an independent durable receipt path."""

    if config.run_id != SCHEDULE_RUN_ID:
        return config
    return replace(config, run_id=workflow_run_id)


def completed_stage_receipt(receipt_path: Path) -> dict[str, object] | None:
    """Return a durable complete receipt that makes a stage resumable."""

    if not receipt_path.is_file():
        return None
    receipt = read_json(receipt_path)
    return receipt if receipt.get("status") == "complete" else None


def raise_for_stage_result(
    *,
    stage: str,
    return_code: int,
    receipt_path: Path,
) -> dict[str, object]:
    """Return a complete receipt or classify the activity failure for Temporal."""

    receipt = read_json(receipt_path) if receipt_path.is_file() else None
    if receipt is not None and receipt.get("status") != "complete":
        raise ApplicationError(
            f"stage={stage} gate_failures={receipt.get('gate_failures')}",
            type="HiraGateError",
            non_retryable=True,
        )
    if return_code != 0 or receipt is None:
        raise ApplicationError(
            f"stage={stage} return_code={return_code} receipt={receipt_path}",
            type="HiraStageError",
            non_retryable=False,
        )
    return receipt


@activity.defn(name="run_hira_benefit_stage")
async def run_hira_benefit_stage(request: HiraStageRequest) -> dict[str, object]:
    if request.stage not in ACTIVITY_STAGES:
        raise ApplicationError(
            f"unknown HIRA stage: {request.stage}",
            type="HiraConfigurationError",
            non_retryable=True,
        )
    config = request.config
    root = run_dir(config.state_root, config.run_id)
    receipt_name = stage_receipt_name(
        request.stage,
        page_start=request.page_start,
        page_end=request.page_end,
    )
    receipt_path = root / f"{receipt_name}.receipt.json"
    completed = completed_stage_receipt(receipt_path)
    if completed is not None:
        return completed
    config_path = root / "workflow_input.json"
    if not config_path.exists():
        write_json(config_path, asdict(config))
    command = [
        "python",
        "-m",
        "pipeline.scripts.crawler.hira_benefit.stage_cli",
        "--stage",
        request.stage,
        "--config-json",
        str(config_path),
    ]
    if request.stage == "discover_page_batch":
        command += [
            "--page-start",
            str(request.page_start),
            "--page-end",
            str(request.page_end),
        ]
    return_code = await run_subprocess_with_heartbeat(
        tuple(command),
        cwd=config.repo_root,
        heartbeat=activity.heartbeat,
        stage=request.stage,
        log_path=stage_log_path(root, receipt_name),
    )
    return raise_for_stage_result(
        stage=request.stage,
        return_code=return_code,
        receipt_path=receipt_path,
    )


@workflow.defn(name=WORKFLOW_NAME)
class HiraBenefitDailyWorkflow:
    async def _stage(
        self,
        config: HiraWorkflowInput,
        stage: str,
        *,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> dict[str, object]:
        policy = ACTIVITY_POLICIES[stage]
        return await workflow.execute_activity(
            "run_hira_benefit_stage",
            HiraStageRequest(
                config=config,
                stage=stage,
                page_start=page_start,
                page_end=page_end,
            ),
            start_to_close_timeout=policy.start_to_close,
            heartbeat_timeout=policy.heartbeat_timeout,
            retry_policy=RetryPolicy(
                maximum_attempts=policy.maximum_attempts,
                initial_interval=timedelta(seconds=15),
                maximum_interval=timedelta(minutes=2),
                backoff_coefficient=2.0,
            ),
        )

    @workflow.run
    async def run(self, config: HiraWorkflowInput) -> dict[str, object]:
        config = resolve_run_config(
            config,
            workflow_run_id=workflow.info().run_id,
        )
        receipts: list[dict[str, object]] = []
        if config.manifest_path is None:
            # Enumeration is split so no single activity owns every list page.
            # Batches run sequentially: the point is to bound each activity, not
            # to raise the request rate against HIRA.
            probe = await self._stage(config, "discover_probe")
            receipts.append(probe)
            total_pages = int(probe["total_pages"])  # type: ignore[arg-type]
            for page_start, page_end in page_batches(
                total_pages,
                config.pages_per_batch,
            ):
                receipts.append(
                    await self._stage(
                        config,
                        "discover_page_batch",
                        page_start=page_start,
                        page_end=page_end,
                    )
                )
        receipts.append(await self._stage(config, "discover_reduce"))
        for stage in POST_DISCOVERY_STAGES:
            receipts.append(await self._stage(config, stage))
        return {
            "run_id": config.run_id,
            "status": "complete",
            "stages": receipts,
        }
