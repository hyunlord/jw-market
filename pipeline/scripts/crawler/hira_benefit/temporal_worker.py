"""Worker entrypoint for a future, separately approved HIRA deployment."""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .temporal_workflow import (
    TASK_QUEUE,
    HiraBenefitDailyWorkflow,
    run_hira_benefit_stage,
)


async def main() -> None:
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.environ.get("HIRA_TEMPORAL_TASK_QUEUE", TASK_QUEUE),
        workflows=[HiraBenefitDailyWorkflow],
        activities=[run_hira_benefit_stage],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
