"""Canonical Temporal schedule contract for HIRA benefit collection."""

from __future__ import annotations

import argparse
from datetime import timedelta
from functools import partial

import anyio
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

from .contract import (
    SCHEDULE_RUN_ID,
    SCHEDULED_DETAIL_NOTICES,
    HiraWorkflowInput,
    scheduled_workflow_input,
)
from .temporal_workflow import (
    TASK_QUEUE,
    WORKFLOW_NAME,
)

SCHEDULE_ID = "jw-hira-benefit-daily-v1"
SCHEDULE_WORKFLOW_ID = "jw-hira-benefit-daily-v1-run"
TIME_ZONE_NAME = "Asia/Seoul"

__all__ = [
    "SCHEDULED_DETAIL_NOTICES",
    "SCHEDULE_ID",
    "SCHEDULE_RUN_ID",
    "SCHEDULE_WORKFLOW_ID",
    "TIME_ZONE_NAME",
    "build_daily_schedule",
    "create_schedule",
    "scheduled_workflow_input",
]


def build_daily_schedule(
    config: HiraWorkflowInput,
    *,
    cron_expression: str,
) -> Schedule:
    """Build the sole production HIRA schedule with overlap prevention."""

    return Schedule(
        action=ScheduleActionStartWorkflow(
            WORKFLOW_NAME,
            config,
            id=SCHEDULE_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(
                seconds=config.workflow_timeout_seconds,
            ),
        ),
        spec=ScheduleSpec(
            cron_expressions=[cron_expression],
            time_zone_name=TIME_ZONE_NAME,
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def create_schedule(
    *,
    temporal_address: str,
    temporal_namespace: str,
    cron_expression: str,
    state_root: str,
    notice_date_boundary: str,
) -> None:
    """Create the schedule once; an existing ID fails closed."""

    client = await Client.connect(
        temporal_address,
        namespace=temporal_namespace,
    )
    config = scheduled_workflow_input(
        state_root=state_root,
        notice_date_boundary=notice_date_boundary,
    )
    await client.create_schedule(
        SCHEDULE_ID,
        build_daily_schedule(config, cron_expression=cron_expression),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporal-address", required=True)
    parser.add_argument("--temporal-namespace", required=True)
    parser.add_argument("--cron", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--notice-date-boundary", required=True)
    args = parser.parse_args()
    anyio.run(
        partial(
            create_schedule,
            temporal_address=args.temporal_address,
            temporal_namespace=args.temporal_namespace,
            cron_expression=args.cron,
            state_root=args.state_root,
            notice_date_boundary=args.notice_date_boundary,
        )
    )


if __name__ == "__main__":
    main()
