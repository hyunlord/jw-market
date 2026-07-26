"""Temporal workflow for HIRA benefit criteria.

This module defines a worker-loadable workflow only. It deliberately contains
no schedule creation or workflow-start command.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .contract import (
        ACTIVITY_POLICIES,
        ACTIVITY_STAGES,
        HiraWorkflowInput,
    )
    from .receipts import read_json, run_dir, write_json
    from .runtime import run_subprocess_with_heartbeat


TASK_QUEUE = "jw-market-hira-benefit-v1"
WORKFLOW_NAME = "jw_hira_benefit_daily_v1"


@dataclass(frozen=True, slots=True)
class HiraStageRequest:
    config: HiraWorkflowInput
    stage: str


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
    receipt_path = root / f"{request.stage}.receipt.json"
    completed = completed_stage_receipt(receipt_path)
    if completed is not None:
        return completed
    config_path = root / "workflow_input.json"
    if not config_path.exists():
        write_json(config_path, asdict(config))
    command = (
        "python",
        "-m",
        "pipeline.scripts.crawler.hira_benefit.stage_cli",
        "--stage",
        request.stage,
        "--config-json",
        str(config_path),
    )
    return_code = await run_subprocess_with_heartbeat(
        command,
        cwd=config.repo_root,
        heartbeat=activity.heartbeat,
        stage=request.stage,
    )
    return raise_for_stage_result(
        stage=request.stage,
        return_code=return_code,
        receipt_path=receipt_path,
    )


@workflow.defn(name=WORKFLOW_NAME)
class HiraBenefitDailyWorkflow:
    @workflow.run
    async def run(self, config: HiraWorkflowInput) -> dict[str, object]:
        receipts: list[dict[str, object]] = []
        for stage in ACTIVITY_STAGES:
            policy = ACTIVITY_POLICIES[stage]
            receipt = await workflow.execute_activity(
                "run_hira_benefit_stage",
                HiraStageRequest(config=config, stage=stage),
                start_to_close_timeout=policy.start_to_close,
                heartbeat_timeout=policy.heartbeat_timeout,
                retry_policy=RetryPolicy(
                    maximum_attempts=policy.maximum_attempts,
                    initial_interval=timedelta(seconds=15),
                    maximum_interval=timedelta(minutes=2),
                    backoff_coefficient=2.0,
                ),
            )
            receipts.append(receipt)
        return {
            "run_id": config.run_id,
            "status": "complete",
            "stages": receipts,
        }
