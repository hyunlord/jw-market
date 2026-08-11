from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import threading
import time
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import SOURCE_NAMES, PlannerOutput, SourceName, SourceResult


SourceAdapter = Callable[[str], SourceResult]


@dataclass(frozen=True)
class ExecutionOutcome:
    results: tuple[SourceResult, ...]
    trace: dict[str, Any]


class ParallelSourceExecutor:
    """Run read-only source queries concurrently with bounded, session-local reuse."""

    def __init__(
        self,
        *,
        adapters: Mapping[SourceName, SourceAdapter],
        per_tool_timeout_s: float = 30.0,
        total_timeout_s: float = 45.0,
        max_cache_entries: int = 2048,
    ) -> None:
        if set(adapters) != set(SOURCE_NAMES):
            raise ValueError("adapters must cover all seven V4 sources")
        if per_tool_timeout_s <= 0 or total_timeout_s <= 0 or max_cache_entries <= 0:
            raise ValueError("timeouts must be positive")
        self._adapters = dict(adapters)
        self._per_tool_timeout_s = per_tool_timeout_s
        self._total_timeout_s = total_timeout_s
        self._max_cache_entries = max_cache_entries
        self._cache: OrderedDict[tuple[str, str, str], SourceResult] = OrderedDict()
        self._cache_lock = threading.Lock()

    def execute(
        self,
        plan: PlannerOutput,
        *,
        session_id: str,
        total_timeout_s: float | None = None,
        answer_sources: tuple[SourceName, ...] | None = None,
        settle_sources: tuple[SourceName, ...] | None = None,
        soft_deadline_s: float | None = None,
        source_filter: tuple[SourceName, ...] | None = None,
        progress_callback: Callable[[SourceName], None] | None = None,
    ) -> tuple[SourceResult, ...]:
        return self.execute_with_trace(
            plan,
            session_id=session_id,
            total_timeout_s=total_timeout_s,
            answer_sources=answer_sources,
            settle_sources=settle_sources,
            soft_deadline_s=soft_deadline_s,
            source_filter=source_filter,
            progress_callback=progress_callback,
        ).results

    def execute_with_trace(
        self,
        plan: PlannerOutput,
        *,
        session_id: str,
        total_timeout_s: float | None = None,
        answer_sources: tuple[SourceName, ...] | None = None,
        settle_sources: tuple[SourceName, ...] | None = None,
        soft_deadline_s: float | None = None,
        source_filter: tuple[SourceName, ...] | None = None,
        progress_callback: Callable[[SourceName], None] | None = None,
    ) -> ExecutionOutcome:
        started = time.monotonic()
        output: list[SourceResult | None] = []
        pending_specs: list[tuple[int, SourceName, str]] = []
        tool_trace: dict[int, dict[str, Any]] = {}
        quorum_fired = False
        quorum_fire_ms: float | None = None
        query_items = {
            source: queries
            for source, queries in plan.tool_queries.items()
            if source_filter is None or source in source_filter
        }
        max_queries = max((len(queries) for queries in query_items.values()), default=0)
        for query_index in range(max_queries):
            for source in query_items:
                queries = query_items[source]
                if query_index >= len(queries):
                    continue
                query = queries[query_index]
                key = (session_id, source, query)
                with self._cache_lock:
                    cached = self._cache.get(key)
                    if cached is not None:
                        self._cache.move_to_end(key)
                if cached is not None:
                    output.append(cached.model_copy(update={"cache_hit": True}))
                    tool_trace[len(output) - 1] = {
                        "source": source,
                        "query": query,
                        "started_ms": 0.0,
                        "ended_ms": 0.0,
                        "status": cached.status,
                        "cache_hit": True,
                    }
                    if progress_callback is not None:
                        progress_callback(source)
                else:
                    index = len(output)
                    output.append(None)
                    pending_specs.append((index, source, query))

        if not pending_specs:
            return ExecutionOutcome(
                results=tuple(item for item in output if item is not None),
                trace={
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                    "quorum_fired": False,
                    "quorum_fire_ms": None,
                    "tools": list(tool_trace.values()),
                },
            )

        pool = ThreadPoolExecutor(
            max_workers=min(7, len(pending_specs)),
            thread_name_prefix="chat-v4-source",
        )
        futures: dict[Future[SourceResult], tuple[int, SourceName, str, float]] = {}
        for index, source, query in pending_specs:
            submitted = time.monotonic()
            futures[pool.submit(self._run, source, query)] = (index, source, query, submitted)
            tool_trace[index] = {
                "source": source,
                "query": query,
                "started_ms": (submitted - started) * 1000,
                "ended_ms": None,
                "status": "running",
                "cache_hit": False,
            }

        deadline = started + min(
            self._total_timeout_s,
            total_timeout_s if total_timeout_s is not None else self._total_timeout_s,
        )
        try:
            remaining = set(futures)
            while remaining:
                now = time.monotonic()
                if (
                    answer_sources
                    and soft_deadline_s is not None
                    and now - started >= soft_deadline_s
                    and _answer_quorum_met(output, answer_sources)
                    and _sources_settled(output, tool_trace, settle_sources)
                ):
                    quorum_fired = True
                    quorum_fire_ms = (now - started) * 1000
                    for future in tuple(remaining):
                        remaining.remove(future)
                        index, source, query, submitted = futures[future]
                        future.cancel()
                        output[index] = SourceResult(
                            source=source,
                            query=query,
                            status="timeout",
                            elapsed_ms=(now - submitted) * 1000,
                            notice="정답 근거 도착 후 soft deadline으로 미포함",
                        )
                        tool_trace[index].update(
                            ended_ms=(now - started) * 1000,
                            status="timeout",
                        )
                    break
                expired = [
                    future
                    for future in remaining
                    if now - futures[future][3] >= self._per_tool_timeout_s
                ]
                for future in expired:
                    remaining.remove(future)
                    index, source, query, submitted = futures[future]
                    future.cancel()
                    output[index] = SourceResult(
                        source=source,
                        query=query,
                        status="timeout",
                        elapsed_ms=(now - submitted) * 1000,
                        notice="응답 지연으로 미포함",
                    )
                    tool_trace[index].update(
                        ended_ms=(now - started) * 1000,
                        status="timeout",
                    )
                if not remaining:
                    break
                remaining_total = deadline - time.monotonic()
                if remaining_total <= 0:
                    for future in tuple(remaining):
                        remaining.remove(future)
                        index, source, query, submitted = futures[future]
                        future.cancel()
                        output[index] = SourceResult(
                            source=source,
                            query=query,
                            status="timeout",
                            elapsed_ms=(time.monotonic() - submitted) * 1000,
                            notice="전체 응답 시간 상한으로 미포함",
                        )
                        tool_trace[index].update(
                            ended_ms=(time.monotonic() - started) * 1000,
                            status="timeout",
                        )
                    break
                next_tool_deadline = min(
                    self._per_tool_timeout_s - (time.monotonic() - futures[future][3])
                    for future in remaining
                )
                wait_candidates = [remaining_total, next_tool_deadline]
                if answer_sources and soft_deadline_s is not None:
                    soft_remaining = started + soft_deadline_s - time.monotonic()
                    if soft_remaining > 0:
                        wait_candidates.append(soft_remaining)
                done, _ = wait(
                    remaining,
                    timeout=max(0.001, min(wait_candidates)),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    remaining.remove(future)
                    index, source, query, _ = futures[future]
                    result = future.result()
                    output[index] = result
                    tool_trace[index].update(
                        ended_ms=(time.monotonic() - started) * 1000,
                        status=result.status,
                    )
                    if progress_callback is not None:
                        progress_callback(source)
                    with self._cache_lock:
                        key = (session_id, source, query)
                        self._cache[key] = result
                        self._cache.move_to_end(key)
                        while len(self._cache) > self._max_cache_entries:
                            self._cache.popitem(last=False)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return ExecutionOutcome(
            results=tuple(item for item in output if item is not None),
            trace={
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "quorum_fired": quorum_fired,
                "quorum_fire_ms": quorum_fire_ms,
                "tools": [tool_trace[index] for index in sorted(tool_trace)],
            },
        )

    def _run(self, source: SourceName, query: str) -> SourceResult:
        started = time.monotonic()
        try:
            result = self._adapters[source](query)
        except Exception as exc:  # noqa: BLE001 - one failed source must not block synthesis
            return SourceResult(
                source=source,
                query=query,
                status="error",
                elapsed_ms=(time.monotonic() - started) * 1000,
                notice=f"{type(exc).__name__}: {exc}",
            )
        return result.model_copy(
            update={"elapsed_ms": (time.monotonic() - started) * 1000}
        )


def _answer_quorum_met(
    output: list[SourceResult | None],
    answer_sources: tuple[SourceName, ...],
) -> bool:
    return all(
        any(
            result is not None
            and result.source == source
            and result.status == "ok"
            for result in output
        )
        for source in answer_sources
    )


def _sources_settled(
    output: list[SourceResult | None],
    tool_trace: Mapping[int, Mapping[str, Any]],
    settle_sources: tuple[SourceName, ...] | None,
) -> bool:
    if not settle_sources:
        return True
    for source in settle_sources:
        indices = [
            index for index, trace in tool_trace.items() if trace["source"] == source
        ]
        if not indices or any(output[index] is None for index in indices):
            return False
    return True
