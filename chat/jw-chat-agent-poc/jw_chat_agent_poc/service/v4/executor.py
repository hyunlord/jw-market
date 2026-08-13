from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import threading
import time
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    ClinicalTrialConcept,
    PlannerOutput,
    SourceName,
    SourceResult,
)
from jw_chat_agent_poc.service.v4.source_tiers import source_tier


SourceAdapter = Callable[..., SourceResult]
SourceProgressCallback = Callable[[SourceResult], None]


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

    def prepare_plan(
        self,
        plan: PlannerOutput,
        *,
        clinical_query_anchor: str,
    ) -> PlannerOutput:
        """Normalize real ClinicalTrials requests without rewriting planner provenance."""
        preparer = getattr(self._adapters["clinicaltrials"], "prepare_requests", None)
        if not callable(preparer):
            return plan
        prepared = tuple(preparer(clinical_query_anchor, plan.clinical_query_specs))
        if not prepared:
            if plan.clinical_query_specs and any(
                "가" <= character <= "힣" for character in clinical_query_anchor
            ):
                return plan.model_copy(update={"clinical_query_specs": ()})
            return plan
        queries = tuple(query for query, _concept in prepared)
        concepts = tuple(concept for _query, concept in prepared)
        return plan.model_copy(
            update={
                "tool_queries": plan.tool_queries.model_copy(
                    update={"clinicaltrials": queries}
                ),
                "clinical_query_specs": concepts,
            }
        )

    def execute(
        self,
        plan: PlannerOutput,
        *,
        session_id: str,
        total_timeout_s: float | None = None,
        answer_sources: tuple[SourceName, ...] | None = None,
        settle_sources: tuple[SourceName, ...] | None = None,
        soft_deadline_s: float | None = None,
        soft_deadline_exempt_sources: tuple[SourceName, ...] | None = None,
        source_filter: tuple[SourceName, ...] | None = None,
        progress_callback: SourceProgressCallback | None = None,
    ) -> tuple[SourceResult, ...]:
        return self.execute_with_trace(
            plan,
            session_id=session_id,
            total_timeout_s=total_timeout_s,
            answer_sources=answer_sources,
            settle_sources=settle_sources,
            soft_deadline_s=soft_deadline_s,
            soft_deadline_exempt_sources=soft_deadline_exempt_sources,
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
        soft_deadline_exempt_sources: tuple[SourceName, ...] | None = None,
        source_filter: tuple[SourceName, ...] | None = None,
        progress_callback: SourceProgressCallback | None = None,
    ) -> ExecutionOutcome:
        started = time.monotonic()
        deadline = started + min(
            self._total_timeout_s,
            total_timeout_s if total_timeout_s is not None else self._total_timeout_s,
        )
        output: list[SourceResult | None] = []
        pending_specs: list[
            tuple[int, SourceName, str, ClinicalTrialConcept | None]
        ] = []
        tool_trace: dict[int, dict[str, Any]] = {}
        quorum_fired = False
        quorum_fire_ms: float | None = None
        exempt_sources = frozenset(soft_deadline_exempt_sources or ())
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
                clinical_concept = (
                    plan.clinical_query_specs[query_index]
                    if source == "clinicaltrials"
                    and query_index < len(plan.clinical_query_specs)
                    else None
                )
                cache_query = _cache_query(query, clinical_concept)
                key = (session_id, source, cache_query)
                with self._cache_lock:
                    cached = self._cache.get(key)
                    if cached is not None:
                        self._cache.move_to_end(key)
                if cached is not None:
                    cached_result = cached.model_copy(update={"cache_hit": True})
                    output.append(cached_result)
                    tool_trace[len(output) - 1] = {
                        "source": source,
                        "query": query,
                        "started_ms": 0.0,
                        "ended_ms": 0.0,
                        "status": cached.status,
                        "cache_hit": True,
                        "notice": cached.notice,
                        "exclusion_reason": _result_exclusion_reason(cached),
                        "soft_deadline_exempt": source in exempt_sources,
                    }
                    if progress_callback is not None:
                        progress_callback(cached_result)
                else:
                    index = len(output)
                    output.append(None)
                    pending_specs.append((index, source, query, clinical_concept))

        if not pending_specs:
            return ExecutionOutcome(
                results=tuple(item for item in output if item is not None),
                trace={
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                    "quorum_fired": False,
                    "quorum_fire_ms": None,
                    "soft_deadline_exempt_sources": sorted(exempt_sources),
                    "tools": list(tool_trace.values()),
                },
            )

        ordered_specs = sorted(
            pending_specs,
            key=lambda item: (source_tier(plan, item[1]), item[0]),
        )
        tier_zero_indices = {
            index
            for index, source, _query, _concept in ordered_specs
            if source_tier(plan, source) == 0
        }
        unsettled_tier_zero = set(tier_zero_indices)
        tier_zero_gate = threading.Event()
        schedule_lock = threading.Lock()
        actual_starts: dict[int, float] = {}
        cancelled_indices: set[int] = set()
        if not unsettled_tier_zero:
            tier_zero_gate.set()

        def release_tier_zero(index: int) -> None:
            if index not in tier_zero_indices:
                return
            with schedule_lock:
                unsettled_tier_zero.discard(index)
                if not unsettled_tier_zero:
                    tier_zero_gate.set()

        def cancel_scheduled(index: int) -> None:
            with schedule_lock:
                cancelled_indices.add(index)
                started_already = index in actual_starts
            if not started_already:
                release_tier_zero(index)

        def run_scheduled(
            index: int,
            source: SourceName,
            query: str,
            clinical_concept: ClinicalTrialConcept | None,
        ) -> SourceResult:
            if source_tier(plan, source) > 0:
                remaining_budget = max(0.0, deadline - time.monotonic())
                if not tier_zero_gate.wait(timeout=remaining_budget):
                    return SourceResult(
                        source=source,
                        query=query,
                        status="deadline_exceeded",
                        notice="Tier-0 조회 후 남은 응답 예산이 없음",
                    )
            actual_started = time.monotonic()
            with schedule_lock:
                if index in cancelled_indices:
                    return SourceResult(
                        source=source,
                        query=query,
                        status="deadline_exceeded",
                        notice="응답 조립 전에 조회가 제외됨",
                    )
                actual_starts[index] = actual_started
                tool_trace[index].update(
                    started_ms=(actual_started - started) * 1000,
                    status="running",
                    deadline_remaining_ms_at_submit=max(
                        0.0,
                        (deadline - actual_started) * 1000,
                    ),
                )
            try:
                return self._run(source, query, clinical_concept, deadline)
            finally:
                release_tier_zero(index)

        pool = ThreadPoolExecutor(
            # The planner already bounds non-clinical fan-out. ClinicalTrials
            # static concepts must all start together so queued searches do not
            # spend their timeout budget waiting behind the first seven tools.
            max_workers=min(len(pending_specs), 44),
            thread_name_prefix="chat-v4-source",
        )
        futures: dict[
            Future[SourceResult],
            tuple[int, SourceName, str, str, float],
        ] = {}
        for index, source, query, clinical_concept in ordered_specs:
            submitted = time.monotonic()
            cache_query = _cache_query(query, clinical_concept)
            tool_trace[index] = {
                "source": source,
                "query": query,
                "started_ms": None,
                "ended_ms": None,
                "status": "queued",
                "cache_hit": False,
                "notice": None,
                "exclusion_reason": None,
                "soft_deadline_exempt": source in exempt_sources,
                "source_tier": source_tier(plan, source),
                "deadline_remaining_ms_at_submit": max(
                    0.0,
                    (deadline - submitted) * 1000,
                ),
            }
            future = pool.submit(
                run_scheduled,
                index,
                source,
                query,
                clinical_concept,
            )
            futures[future] = (
                index,
                source,
                query,
                cache_query,
                submitted,
            )
        try:
            remaining = set(futures)
            while remaining:
                now = time.monotonic()
                if (
                    not quorum_fired
                    and answer_sources
                    and soft_deadline_s is not None
                    and now - started >= soft_deadline_s
                    and _answer_quorum_met(output, answer_sources)
                    and _sources_settled(output, tool_trace, settle_sources)
                ):
                    quorum_fired = True
                    quorum_fire_ms = (now - started) * 1000
                    for future in tuple(remaining):
                        index, source, query, _cache_query_value, submitted = futures[future]
                        if source in exempt_sources:
                            continue
                        remaining.remove(future)
                        future.cancel()
                        cancel_scheduled(index)
                        actual_started = actual_starts.get(index, submitted)
                        timed_out = SourceResult(
                            source=source,
                            query=query,
                            status="timeout",
                            elapsed_ms=(now - actual_started) * 1000,
                            notice="정답 근거 도착 후 soft deadline으로 미포함",
                        )
                        output[index] = timed_out
                        tool_trace[index].update(
                            ended_ms=(now - started) * 1000,
                            status="timeout",
                            notice=timed_out.notice,
                            exclusion_reason="soft_deadline_after_answer_quorum",
                        )
                        if progress_callback is not None:
                            progress_callback(timed_out)
                    if not remaining:
                        break
                expired = [
                    future
                    for future in remaining
                    if futures[future][0] in actual_starts
                    and now - actual_starts[futures[future][0]]
                    >= self._per_tool_timeout_s
                ]
                for future in expired:
                    remaining.remove(future)
                    index, source, query, _cache_query_value, submitted = futures[future]
                    future.cancel()
                    cancel_scheduled(index)
                    actual_started = actual_starts.get(index, submitted)
                    timed_out = SourceResult(
                        source=source,
                        query=query,
                        status="timeout",
                        elapsed_ms=(now - actual_started) * 1000,
                        notice="응답 지연으로 미포함",
                    )
                    output[index] = timed_out
                    tool_trace[index].update(
                        ended_ms=(now - started) * 1000,
                        status="timeout",
                        notice=timed_out.notice,
                        exclusion_reason="per_tool_timeout",
                    )
                    if progress_callback is not None:
                        progress_callback(timed_out)
                if not remaining:
                    break
                remaining_total = deadline - time.monotonic()
                if remaining_total <= 0:
                    for future in tuple(remaining):
                        remaining.remove(future)
                        index, source, query, _cache_query_value, submitted = futures[future]
                        future.cancel()
                        cancel_scheduled(index)
                        actual_started = actual_starts.get(index, submitted)
                        timed_out = SourceResult(
                            source=source,
                            query=query,
                            status="timeout",
                            elapsed_ms=(time.monotonic() - actual_started) * 1000,
                            notice="전체 응답 시간 상한으로 미포함",
                        )
                        output[index] = timed_out
                        tool_trace[index].update(
                            ended_ms=(time.monotonic() - started) * 1000,
                            status="timeout",
                            notice=timed_out.notice,
                            exclusion_reason="total_timeout",
                        )
                        if progress_callback is not None:
                            progress_callback(timed_out)
                    break
                started_remaining = [
                    actual_starts[futures[future][0]]
                    for future in remaining
                    if futures[future][0] in actual_starts
                ]
                wait_candidates = [remaining_total]
                if started_remaining:
                    wait_candidates.append(
                        min(
                            self._per_tool_timeout_s
                            - (time.monotonic() - actual_started)
                            for actual_started in started_remaining
                        )
                    )
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
                    index, source, query, cache_query, _ = futures[future]
                    result = future.result()
                    output[index] = result
                    tool_trace[index].update(
                        ended_ms=(time.monotonic() - started) * 1000,
                        status=result.status,
                        notice=result.notice,
                        exclusion_reason=_result_exclusion_reason(result),
                    )
                    if progress_callback is not None:
                        progress_callback(result)
                    with self._cache_lock:
                        key = (session_id, source, cache_query)
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
                "soft_deadline_exempt_sources": sorted(exempt_sources),
                "scheduler": {
                    "tier_zero_first": True,
                    "auxiliary_order": "entity_round_robin",
                },
                "tools": [tool_trace[index] for index in sorted(tool_trace)],
            },
        )

    def _run(
        self,
        source: SourceName,
        query: str,
        clinical_concept: ClinicalTrialConcept | None,
        deadline: float,
    ) -> SourceResult:
        started = time.monotonic()
        if deadline - started < 0.05:
            return SourceResult(
                source=source,
                query=query,
                status="deadline_exceeded",
                notice="남은 응답 예산이 최소 파싱 시간보다 짧아 조회하지 않음",
            )
        try:
            result = (
                self._adapters[source](query, concept=clinical_concept)
                if source == "clinicaltrials" and clinical_concept is not None
                else self._adapters[source](query)
            )
        except Exception as exc:  # noqa: BLE001 - one failed source must not block synthesis
            return SourceResult(
                source=source,
                query=query,
                status="upstream",
                elapsed_ms=(time.monotonic() - started) * 1000,
                notice=f"{type(exc).__name__}: {exc}",
            )
        return result.model_copy(
            update={"elapsed_ms": (time.monotonic() - started) * 1000}
        )


def _cache_query(
    query: str,
    concept: ClinicalTrialConcept | None,
) -> str:
    if concept is None:
        return query
    return f"{query}\x1f{concept.model_dump_json()}"


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


def _result_exclusion_reason(result: SourceResult) -> str | None:
    if result.status == "ok":
        return None
    notice = (result.notice or "").casefold()
    if any(
        token in notice
        for token in ("quota", "usage limit", "usage_limit", "plan's set usage", "사용량")
    ):
        return "provider_quota"
    if result.status == "quota":
        return "provider_quota"
    if result.status in {"timeout", "deadline_exceeded"}:
        return "upstream_timeout"
    if result.status == "empty":
        return "empty_result"
    if result.status == "parse_error":
        return "parse_error"
    if result.status == "scope_limit":
        return "scope_limit"
    return "upstream_error"
