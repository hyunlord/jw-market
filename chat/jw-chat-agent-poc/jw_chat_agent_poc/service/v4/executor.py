from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
import threading
import time

from jw_chat_agent_poc.service.v4.contracts import SOURCE_NAMES, PlannerOutput, SourceName, SourceResult


SourceAdapter = Callable[[str], SourceResult]


class ParallelSourceExecutor:
    """Run read-only source queries concurrently with bounded, session-local reuse."""

    def __init__(
        self,
        *,
        adapters: Mapping[SourceName, SourceAdapter],
        per_tool_timeout_s: float = 10.0,
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
        soft_deadline_s: float | None = None,
    ) -> tuple[SourceResult, ...]:
        started = time.monotonic()
        output: list[SourceResult | None] = []
        pending_specs: list[tuple[int, SourceName, str]] = []
        query_items = dict(plan.tool_queries.items())
        max_queries = max(len(queries) for queries in query_items.values())
        for query_index in range(max_queries):
            for source in SOURCE_NAMES:
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
                else:
                    index = len(output)
                    output.append(None)
                    pending_specs.append((index, source, query))

        if not pending_specs:
            return tuple(item for item in output if item is not None)

        pool = ThreadPoolExecutor(
            max_workers=min(7, len(pending_specs)),
            thread_name_prefix="chat-v4-source",
        )
        futures: dict[Future[SourceResult], tuple[int, SourceName, str, float]] = {}
        for index, source, query in pending_specs:
            submitted = time.monotonic()
            futures[pool.submit(self._run, source, query)] = (index, source, query, submitted)

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
                ):
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
                    break
                next_tool_deadline = min(
                    self._per_tool_timeout_s - (time.monotonic() - futures[future][3])
                    for future in remaining
                )
                done, _ = wait(
                    remaining,
                    timeout=max(
                        0.001,
                        min(
                            remaining_total,
                            next_tool_deadline,
                            max(0.001, started + soft_deadline_s - time.monotonic())
                            if answer_sources and soft_deadline_s is not None
                            else remaining_total,
                        ),
                    ),
                )
                for future in done:
                    remaining.remove(future)
                    index, source, query, _ = futures[future]
                    result = future.result()
                    output[index] = result
                    with self._cache_lock:
                        key = (session_id, source, query)
                        self._cache[key] = result
                        self._cache.move_to_end(key)
                        while len(self._cache) > self._max_cache_entries:
                            self._cache.popitem(last=False)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return tuple(item for item in output if item is not None)

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
