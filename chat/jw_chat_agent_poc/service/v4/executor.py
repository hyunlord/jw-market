from __future__ import annotations

import inspect
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    ClinicalTrialConcept,
    PlannerOutput,
    SourceName,
    SourceResult,
    tool_query_sources,
)
from jw_chat_agent_poc.service.v4.source_tiers import source_tier


SourceAdapter = Callable[..., SourceResult]
SourceProgressCallback = Callable[[SourceResult], None]
TransportEventCallback = Callable[[Mapping[str, Any]], None]


class ExecutionCallDeduper:
    """Share identical physical calls only within one executor invocation."""

    def __init__(self, *, deadline: float | None = None) -> None:
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, ...], Future[tuple[Any, ...]]] = {}
        self._deadline = deadline

    def run(
        self,
        key: tuple[str, ...],
        call: Callable[[], Any],
        *,
        timeout_s: float | None = None,
    ) -> tuple[Any, bool]:
        with self._lock:
            future = self._calls.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._calls[key] = future
        if not owner:
            shared = future.result(timeout=self._remaining_timeout(timeout_s))
            return deepcopy(shared[0]), False
        try:
            result = _materialize_shared_result(call())
        except Exception as exc:
            future.set_exception(exc)
            raise
        future.set_result((result,))
        return deepcopy(result), True

    def _remaining_timeout(self, timeout_s: float | None) -> float | None:
        remaining = (
            max(0.0, self._deadline - time.monotonic())
            if self._deadline is not None
            else None
        )
        if timeout_s is None:
            return remaining
        return (
            max(0.0, min(timeout_s, remaining))
            if remaining is not None
            else timeout_s
        )


def _materialize_shared_result(result: Any) -> Any:
    if isinstance(result, Iterator):
        return tuple(result)
    return deepcopy(result)


def _quorum_early_exit_enabled() -> bool:
    """Whether a satisfied answer-source quorum may cancel the lanes still running.

    Off by default. The quorum is measured against ``answer_sources``, which the planner
    collapses to a single keyword-derived source, so six seconds after the wave starts
    one finished lane could cancel every other one. That is how a question reached the
    user on two lanes when six had evidence to give. Cancelling on a soft deadline trades
    evidence for latency, and this round trades the other way. Set
    CHAT_V4_ANSWER_QUORUM_EARLY_EXIT=1 to restore it.
    """

    return os.environ.get("CHAT_V4_ANSWER_QUORUM_EARLY_EXIT", "0").strip().casefold() in {
        "1",
        "true",
        "on",
        "yes",
    }


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

    def clinical_molecule_fallback(self, brand: str) -> tuple[tuple[str, ...], str]:
        resolver = getattr(self._adapters["clinicaltrials"], "molecule_fallback", None)
        if not callable(resolver):
            return (), "failed"
        molecules, status = resolver(brand)
        return tuple(molecules), str(status)

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
            prepared = tuple(
                (query, concept)
                for concept in plan.clinical_query_specs
                if concept.search_area == "condition"
                for raw_query in concept.source_queries
                if (query := " ".join(str(raw_query).split()))
            )
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
        call_deduper = ExecutionCallDeduper(deadline=deadline)
        output: list[SourceResult | None] = []
        pending_specs: list[
            tuple[int, SourceName, str, ClinicalTrialConcept | None]
        ] = []
        tool_trace: dict[int, dict[str, Any]] = {}
        quorum_fired = False
        quorum_fire_ms: float | None = None
        quorum_early_exit = _quorum_early_exit_enabled()
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
                    "quorum_early_exit_enabled": quorum_early_exit,
                    "soft_deadline_exempt_sources": sorted(exempt_sources),
                    "tools": list(tool_trace.values()),
                },
            )

        clinical_start_barrier = (
            threading.Barrier(len(pending_specs))
            if len(pending_specs) > 1
            and all(source == "clinicaltrials" for _index, source, _query, _concept in pending_specs)
            else None
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
            if clinical_start_barrier is not None:
                try:
                    clinical_start_barrier.wait(
                        timeout=max(0.0, deadline - time.monotonic())
                    )
                except threading.BrokenBarrierError:
                    return SourceResult(
                        source=source,
                        query=query,
                        status="deadline_exceeded",
                        notice="ClinicalTrials 동시 시작 게이트가 응답 기한 전에 열리지 않음",
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

            def record_transport_event(event: Mapping[str, Any]) -> None:
                with schedule_lock:
                    _merge_transport_event(tool_trace[index], event)

            try:
                return self._run(
                    source,
                    query,
                    clinical_concept,
                    deadline,
                    requested_answer_shape=plan.requested_answer_shape,
                    transport_event_callback=record_transport_event,
                    call_deduper=call_deduper,
                )
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
                    and quorum_early_exit
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
                if quorum_early_exit and answer_sources and soft_deadline_s is not None:
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
                "quorum_early_exit_enabled": quorum_early_exit,
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
        *,
        requested_answer_shape: Any | None = None,
        transport_event_callback: TransportEventCallback | None = None,
        call_deduper: ExecutionCallDeduper | None = None,
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
            adapter = self._adapters[source]
            if source == "clinicaltrials" and clinical_concept is not None:
                result = adapter(query, concept=clinical_concept)
            elif (
                source == "mart"
                and requested_answer_shape is not None
                and _accepts_period_bounds(adapter)
            ):
                # The mart adapter walks several brands inside this one budget.
                # Telling it how much time it actually has lets it hand back the
                # brands it finished instead of being cut with all of them lost.
                budget_kwargs = (
                    {
                        "budget_s": max(
                            0.0,
                            min(self._per_tool_timeout_s, deadline - started),
                        )
                    }
                    if _accepts_retrieval_budget(adapter)
                    else {}
                )
                result = adapter(
                    query,
                    period_from=requested_answer_shape.period_from,
                    period_to=requested_answer_shape.period_to,
                    **budget_kwargs,
                )
            elif getattr(adapter, "supports_call_deduper", False):
                result = adapter(
                    query,
                    call_deduper=call_deduper,
                    call_timeout_s=max(
                        0.0,
                        min(self._per_tool_timeout_s, deadline - time.monotonic()),
                    ),
                    **(
                        {"transport_event_callback": transport_event_callback}
                        if getattr(adapter, "supports_transport_event_callback", False)
                        else {}
                    ),
                )
            elif getattr(adapter, "supports_transport_event_callback", False):
                result = adapter(query, transport_event_callback=transport_event_callback)
            else:
                result = adapter(query)
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


def _accepts_retrieval_budget(adapter: Any) -> bool:
    """Only adapters that name ``budget_s`` are told the budget.

    Deliberately narrower than :func:`_accepts_period_bounds`: a ``**kwargs``
    test double would silently swallow the argument and report a preservation
    behaviour it never implemented.
    """
    try:
        parameters = inspect.signature(adapter).parameters
    except (TypeError, ValueError):
        return False
    return "budget_s" in parameters


def _accepts_period_bounds(adapter: Any) -> bool:
    try:
        parameters = inspect.signature(adapter).parameters.values()
    except (TypeError, ValueError):
        return False
    names = {parameter.name for parameter in parameters}
    return (
        {"period_from", "period_to"}.issubset(names)
        or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    )


def _merge_transport_event(
    tool_trace: dict[str, Any],
    event: Mapping[str, Any],
) -> None:
    policy = tool_trace.setdefault(
        "web_transport",
        {
            "attempt_trace": [],
            "requests_issued": 0,
            "responses_received": 0,
            "retry_count": 0,
            "read_timeout_retries": 0,
            "credit_at_risk_without_response": 0,
            "pending_attempts": 0,
            "retry_scope": "connect_or_5xx_only",
        },
    )
    attempt = int(event.get("attempt") or 0)
    attempts = policy["attempt_trace"]
    current = next((item for item in attempts if item["attempt"] == attempt), None)
    if current is None:
        current = {"attempt": attempt}
        attempts.append(current)
        attempts.sort(key=lambda item: item["attempt"])
    current.update(dict(event))

    issued = [item for item in attempts if bool(item.get("request_issued"))]
    completed = [item for item in issued if item.get("phase") == "attempt_completed"]
    policy.update(
        {
            "requests_issued": len(issued),
            "responses_received": sum(
                bool(item.get("response_received")) for item in completed
            ),
            "retry_count": max(len(issued) - 1, 0),
            "read_timeout_retries": sum(
                1
                for previous, following in zip(issued, issued[1:], strict=False)
                if previous.get("error_type") == "read_timeout"
                and bool(following.get("request_issued"))
            ),
            "credit_at_risk_without_response": sum(
                not bool(item.get("response_received")) for item in issued
            ),
            "pending_attempts": sum(
                item.get("phase") != "attempt_completed" for item in issued
            ),
        }
    )
    for key in (
        "search_depth",
        "topic",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "max_concurrency",
    ):
        if key in event:
            policy[key] = event[key]


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
        for source in tool_query_sources(answer_sources)
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
    failure_class = str(result.failure_detail.get("failure_class") or "").casefold()
    if failure_class == "quota" or result.status == "quota" or result.failure_reason in {
        "RATE_LIMITED",
        "QUOTA_EXCEEDED",
    }:
        return "provider_quota"
    if failure_class == "timeout" or result.status in {"timeout", "deadline_exceeded"}:
        return "upstream_timeout"
    if failure_class == "0_results" or result.status == "empty":
        return "empty_result"
    if result.status == "parse_error":
        return "parse_error"
    if result.status == "scope_limit":
        return "scope_limit"
    return "upstream_error"
