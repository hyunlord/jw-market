"""Manually start one JW crawl shadow workflow; this command creates no schedule."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta

from temporalio.client import Client

from pipeline.scripts.crawler.crawl_temporal_contract import CrawlDailyInput
from pipeline.scripts.crawler.temporal_crawl_daily import (
    CrawlDailyWorkflow,
    NAMESPACE,
    TASK_QUEUE,
)


async def run(args: argparse.Namespace) -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", NAMESPACE),
    )
    config = CrawlDailyInput(
        run_id=args.run_id,
        command_revision=args.command_revision,
        inject_failure_stage=args.inject_failure_stage,
        inject_reported_failures_stage=args.inject_reported_failures_stage,
        inject_heartbeat_stall_stage=args.inject_heartbeat_stall_stage,
        test_heartbeat_timeout_seconds=args.test_heartbeat_timeout_seconds,
    ).validated()
    result = await client.execute_workflow(
        CrawlDailyWorkflow.run,
        config,
        id=args.workflow_id,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", TASK_QUEUE),
        execution_timeout=timedelta(hours=16),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--command-revision", required=True)
    parser.add_argument("--inject-failure-stage")
    parser.add_argument("--inject-reported-failures-stage")
    parser.add_argument("--inject-heartbeat-stall-stage")
    parser.add_argument("--test-heartbeat-timeout-seconds", type=int)
    return parser


if __name__ == "__main__":
    asyncio.run(run(build_parser().parse_args()))
