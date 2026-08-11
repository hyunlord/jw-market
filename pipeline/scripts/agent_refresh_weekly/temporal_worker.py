"""Temporal workflow and worker entry point for weekly Agent refresh."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

from pipeline.scripts.agent_refresh_weekly.activities import (
    agent_job_image_is_configured,
    preflight_activity,
    run_stage_activity,
)
from pipeline.scripts.agent_refresh_weekly.contract import (
    STAGE_ORDER,
    TASK_QUEUE,
    WORKFLOW_TYPE,
)


@workflow.defn(name=WORKFLOW_TYPE)
class WeeklyAgentRefreshWorkflow:
    @workflow.run
    async def run(self) -> dict[str, Any]:
        workflow_id = workflow.info().workflow_id
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=30),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=2,
        )
        preflight = await workflow.execute_activity(
            preflight_activity,
            {"workflow_id": workflow_id},
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry,
        )
        if preflight["status"] == "skipped":
            return {"status": "skipped", "preflight": preflight, "stages": []}
        stages = []
        for stage in STAGE_ORDER:
            result = await workflow.execute_activity(
                run_stage_activity,
                {"stage": stage, "workflow_id": workflow_id},
                start_to_close_timeout=(
                    timedelta(hours=7)
                    if stage in {"agent2-short", "agent2-long"}
                    else timedelta(hours=3)
                ),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=retry,
            )
            stages.append(result)
            if result["status"] == "skipped":
                return {"status": "skipped", "preflight": preflight, "stages": stages}
        finalizer = next(
            (item for item in stages if item["stage"] == "agent2-finalize"),
            None,
        )
        verdict = ((finalizer or {}).get("output") or {}).get("verdict", "complete")
        return {"status": verdict, "preflight": preflight, "stages": stages}


async def _main() -> None:
    if not agent_job_image_is_configured():
        raise RuntimeError("AGENT_JOB_IMAGE is required")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", TASK_QUEUE),
        workflows=[WeeklyAgentRefreshWorkflow],
        activities=[preflight_activity, run_stage_activity],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
