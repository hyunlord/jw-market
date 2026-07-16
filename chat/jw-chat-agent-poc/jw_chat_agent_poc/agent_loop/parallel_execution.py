from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import time
from typing import Generic, Literal, TypeVar

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan


T = TypeVar("T")
ExecutionMode = Literal["serial", "parallel"]
PARALLEL_TOOL_WORKERS_ENV = "CHAT_BQ_PARALLEL_TOOL_WORKERS"
_PARALLEL_SAFE_TOOLS = frozenset(
    {
        "search_news",
        "get_disease_stats",
        "get_procedure_stats",
        "search_clinical",
        "search_patent",
        "search_drug_info",
        "search_safety",
        "csd_activity_trend",
        "web_search",
    }
)


@dataclass(frozen=True, slots=True)
class TimedExecution(Generic[T]):
    plan: ToolCallPlan
    result: T
    elapsed_ms: float
    mode: ExecutionMode


def execute_tool_batch(
    plans: Sequence[ToolCallPlan],
    execute: Callable[[ToolCallPlan], T],
    *,
    max_workers: int | None = None,
) -> tuple[TimedExecution[T], ...]:
    """Run independent read-only support tools concurrently and preserve plan order."""

    workers = _worker_count(max_workers)
    results: list[TimedExecution[T] | None] = [None] * len(plans)
    parallel_indexes = [index for index, plan in enumerate(plans) if plan.name in _PARALLEL_SAFE_TOOLS]
    serial_indexes = [index for index in range(len(plans)) if index not in parallel_indexes]

    for index in serial_indexes:
        results[index] = _timed(plans[index], execute, "serial")

    if len(parallel_indexes) < 2 or workers == 1:
        for index in parallel_indexes:
            results[index] = _timed(plans[index], execute, "serial")
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(parallel_indexes)), thread_name_prefix="bq-tool") as pool:
            futures = {
                index: pool.submit(_timed, plans[index], execute, "parallel")
                for index in parallel_indexes
            }
            for index, future in futures.items():
                results[index] = future.result()

    return tuple(item for item in results if item is not None)


def _timed(
    plan: ToolCallPlan,
    execute: Callable[[ToolCallPlan], T],
    mode: ExecutionMode,
) -> TimedExecution[T]:
    started = time.perf_counter()
    result = execute(plan)
    return TimedExecution(
        plan=plan,
        result=result,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        mode=mode,
    )


def _worker_count(value: int | None) -> int:
    if value is not None:
        return max(1, min(int(value), 8))
    try:
        configured = int(os.environ.get(PARALLEL_TOOL_WORKERS_ENV, "3"))
    except ValueError:
        configured = 3
    return max(1, min(configured, 8))
