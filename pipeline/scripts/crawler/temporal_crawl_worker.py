"""Run the JW crawl Temporal worker without creating a schedule."""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from pipeline.scripts.crawler.temporal_crawl_daily import (
    ALL_ACTIVITIES,
    ALL_WORKFLOWS,
    NAMESPACE,
    TASK_QUEUE,
)


async def main() -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", NAMESPACE),
    )
    worker = Worker(
        client,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", TASK_QUEUE),
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
