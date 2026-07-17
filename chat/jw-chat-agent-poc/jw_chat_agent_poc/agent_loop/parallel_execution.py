from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    additional_parallel_tools: Collection[str] = (),
    on_complete: Callable[[TimedExecution[T]], None] | None = None,
) -> tuple[TimedExecution[T], ...]:
    """Run independent read-only support tools concurrently and preserve plan order."""

    parallel_indexes, workers = _parallel_execution_plan(
        plans,
        max_workers=max_workers,
        additional_parallel_tools=additional_parallel_tools,
    )
    results: list[TimedExecution[T] | None] = [None] * len(plans)
    serial_indexes = [index for index in range(len(plans)) if index not in parallel_indexes]

    for index in serial_indexes:
        item = _timed(plans[index], execute, "serial")
        results[index] = item
        if on_complete is not None:
            on_complete(item)

    if len(parallel_indexes) < 2 or workers == 1:
        for index in parallel_indexes:
            item = _timed(plans[index], execute, "serial")
            results[index] = item
            if on_complete is not None:
                on_complete(item)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(parallel_indexes)), thread_name_prefix="bq-tool") as pool:
            futures = {
                pool.submit(_timed, plans[index], execute, "parallel"): index
                for index in parallel_indexes
            }
            for future in as_completed(futures):
                index = futures[future]
                item = future.result()
                results[index] = item
                if on_complete is not None:
                    on_complete(item)

    return tuple(item for item in results if item is not None)


def planned_parallel_tool_names(
    plans: Sequence[ToolCallPlan],
    *,
    max_workers: int | None = None,
    additional_parallel_tools: Collection[str] = (),
) -> frozenset[str]:
    """Return tools that will actually enter the concurrent executor."""

    indexes, workers = _parallel_execution_plan(
        plans,
        max_workers=max_workers,
        additional_parallel_tools=additional_parallel_tools,
    )
    if len(indexes) < 2 or workers == 1:
        return frozenset()
    return frozenset(plans[index].name for index in indexes)


def _parallel_execution_plan(
    plans: Sequence[ToolCallPlan],
    *,
    max_workers: int | None,
    additional_parallel_tools: Collection[str],
) -> tuple[list[int], int]:
    parallel_tools = _PARALLEL_SAFE_TOOLS.union(additional_parallel_tools)
    indexes = [index for index, plan in enumerate(plans) if plan.name in parallel_tools]
    return indexes, _worker_count(max_workers)


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
