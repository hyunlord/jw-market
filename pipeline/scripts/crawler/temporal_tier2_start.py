"""Start or inspect the isolated Tier2 Temporal pilot workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta

from temporalio.client import Client

from pipeline.scripts.crawler.temporal_tier2_pilot import (
    HelloWorkflow,
    NAMESPACE,
    PilotInput,
    TASK_QUEUE,
    Tier2PilotWorkflow,
)


async def run(args: argparse.Namespace) -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", NAMESPACE),
    )
    if args.command == "hello":
        result = await client.execute_workflow(
            HelloWorkflow.run,
            args.name,
            id=args.workflow_id,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", TASK_QUEUE),
            execution_timeout=timedelta(minutes=5),
        )
    else:
        config = PilotInput(
            run_id=args.run_id,
            brand_file=args.brand_file,
            weekday_slice=args.weekday_slice,
            sites=args.sites,
            inject_failure_stage=args.inject_failure_stage,
            inject_failure_attempts=args.inject_failure_attempts,
        )
        result = await client.execute_workflow(
            Tier2PilotWorkflow.run,
            config,
            id=args.workflow_id,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", TASK_QUEUE),
            execution_timeout=timedelta(hours=8),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    hello = subparsers.add_parser("hello")
    hello.add_argument("--name", default="jw-market")
    hello.add_argument("--workflow-id", default="jw-market-hello-pilot")
    pilot = subparsers.add_parser("pilot")
    pilot.add_argument("--run-id", required=True)
    pilot.add_argument("--workflow-id", required=True)
    pilot.add_argument("--brand-file", required=True)
    pilot.add_argument("--weekday-slice", type=int, choices=range(7), required=True)
    pilot.add_argument("--sites", default="약업신문")
    pilot.add_argument("--inject-failure-stage")
    pilot.add_argument("--inject-failure-attempts", type=int, default=0)
    return parser


if __name__ == "__main__":
    asyncio.run(run(build_parser().parse_args()))
