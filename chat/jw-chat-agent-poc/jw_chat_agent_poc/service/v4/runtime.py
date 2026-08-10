from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Sequence
import threading
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.adapters import build_source_adapters
from jw_chat_agent_poc.service.v4.contracts import SourceResult, V4Answer
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.llm import planner_client, synthesizer_client
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome, V4Synthesizer


_PUBLIC_SOURCE = {
    "mart": "내부 데이터마트",
    "nedrug": "식품의약품안전처",
    "hira": "HIRA",
    "openfda": "FDA",
    "clinicaltrials": "ClinicalTrials.gov",
    "web": "웹 자료",
    "patent": "특허 자료",
}


class V4Runtime:
    def __init__(
        self,
        *,
        planner: V4Planner,
        executor: ParallelSourceExecutor,
        synthesizer: V4Synthesizer,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._synthesizer = synthesizer
        self._total_timeout_s = 54.0
        self._session_results: OrderedDict[str, tuple[SourceResult, ...]] = OrderedDict()
        self._session_results_lock = threading.Lock()
        self._max_session_results = 1024

    def answer(
        self,
        question: str,
        *,
        conversation_id: str | None,
        turns: Sequence[ConversationTurn],
    ) -> V4Answer:
        started = time.monotonic()
        deadline = started + self._total_timeout_s
        selected_turns = tuple(turns)[-10:]
        session_id = conversation_id or uuid4().hex
        plan_with_trace = getattr(self._planner, "plan_with_trace", None)
        if callable(plan_with_trace):
            planner_outcome = plan_with_trace(
                question,
                selected_turns,
                budget_s=min(18.0, _remaining(deadline)),
            )
        else:
            planner_started = time.monotonic()
            planner_outcome = SimpleNamespace(
                plan=self._planner.plan(
                    question,
                    selected_turns,
                    budget_s=min(18.0, _remaining(deadline)),
                ),
                trace={
                    "status": "unknown",
                    "elapsed_ms": (time.monotonic() - planner_started) * 1000,
                    "usage": _empty_usage(),
                },
            )
        plan = planner_outcome.plan
        prior_results = self._get_session_results(session_id)
        can_reuse_prior = (
            prior_results
            and _is_prior_result_reference(question)
            and _prior_results_cover_answer_sources(plan.answer_sources, prior_results)
        )
        if can_reuse_prior:
            first_execution = SimpleNamespace(
                results=tuple(
                    result.model_copy(update={"cache_hit": True})
                    for result in prior_results
                ),
                trace={
                    "elapsed_ms": 0.0,
                    "quorum_fired": False,
                    "quorum_fire_ms": None,
                    "tools": [],
                    "session_result_reused": True,
                },
            )
        else:
            first_execution = _execute_with_trace(
                self._executor,
                plan,
                session_id=session_id,
                total_timeout_s=min(20.0, _remaining(deadline)),
                answer_sources=plan.answer_sources,
                soft_deadline_s=6.0,
            )
            first_execution.trace.setdefault("session_result_reused", False)
        first_results = first_execution.results
        linked_plan = (
            self._planner.link(
                plan,
                first_results,
                selected_turns,
                budget_s=min(7.0, _remaining(deadline)),
            )
            if (
                plan.needs_second_hop
                and not first_execution.trace["session_result_reused"]
                and _remaining(deadline) > 1.0
            )
            else None
        )
        linked_execution = (
            _execute_with_trace(
                self._executor,
                linked_plan,
                session_id=session_id,
                total_timeout_s=min(10.0, _remaining(deadline)),
                answer_sources=linked_plan.answer_sources,
                soft_deadline_s=6.0,
            )
            if linked_plan is not None and _remaining(deadline) > 0.1
            else SimpleNamespace(results=(), trace=None)
        )
        linked_results = linked_execution.results
        current_results = (*first_results, *linked_results)
        if prior_results and (
            _is_causal_followup(question)
            or (_is_prior_result_reference(question) and not can_reuse_prior)
        ):
            current_results = _merge_results(prior_results, current_results)
        results = tuple(_mark_citations_used(result) for result in current_results)
        self._remember_session_results(session_id, results)
        synthesize_with_trace = getattr(self._synthesizer, "synthesize_with_trace", None)
        if callable(synthesize_with_trace):
            synthesis = synthesize_with_trace(
                plan,
                results,
                selected_turns,
                budget_s=min(24.0, _remaining(deadline)),
            )
        else:
            synthesis = SynthesisOutcome(
                text=self._synthesizer.synthesize(
                    plan,
                    results,
                    selected_turns,
                    budget_s=min(24.0, _remaining(deadline)),
                ),
                trace={},
            )
        gated = apply_v4_gates(plan.resolved_question, synthesis.text, results)
        elapsed_ms = (time.monotonic() - started) * 1000
        synth_usage = _normalized_synth_usage(synthesis.trace)
        planner_usage = planner_outcome.trace.get("usage") or _empty_usage()
        stage_timing = {
            "planner_elapsed_ms": planner_outcome.trace.get("elapsed_ms"),
            "wave_elapsed_ms": first_execution.trace.get("elapsed_ms"),
            "link_wave_elapsed_ms": (
                linked_execution.trace.get("elapsed_ms")
                if isinstance(linked_execution.trace, dict)
                else None
            ),
            "synth_elapsed_ms": synthesis.trace.get("elapsed_ms"),
            "total_elapsed_ms": elapsed_ms,
        }
        trace = {
            "v4": True,
            "planner_serving": getattr(self._planner, "serving_id", "unknown"),
            "fallback": plan.linking_plan.startswith("planner fallback;"),
            "planner": plan.model_dump(mode="json"),
            "planner_usage": planner_usage,
            "second_hop": linked_plan.model_dump(mode="json") if linked_plan else None,
            "tool_results": [
                {
                    "source": result.source,
                    "query": result.query,
                    "status": result.status,
                    "elapsed_ms": result.elapsed_ms,
                    "cache_hit": result.cache_hit,
                    "notice": result.notice,
                    "citations": [
                        citation.model_dump(mode="json")
                        for citation in result.citations
                    ],
                    "payload": result.payload,
                }
                for result in results
            ],
            "synthesizer": synthesis.trace,
            "synth_usage": synth_usage,
            "execution": {
                **first_execution.trace,
                "link_wave": linked_execution.trace,
            },
            "stage_timing": stage_timing,
            "gates": gated.trace,
        }
        sources = tuple(
            dict.fromkeys(
                citation.source
                for result in results
                if result.status == "ok"
                for citation in result.citations
            )
        )
        return V4Answer(
            text=gated.text,
            sources=sources,
            trace=trace,
            timing=stage_timing,
            conversation_id=session_id,
        )

    def _get_session_results(self, session_id: str) -> tuple[SourceResult, ...]:
        with self._session_results_lock:
            results = self._session_results.get(session_id, ())
            if results:
                self._session_results.move_to_end(session_id)
            return results

    def _remember_session_results(
        self,
        session_id: str,
        results: tuple[SourceResult, ...],
    ) -> None:
        reusable = tuple(result for result in results if result.status == "ok")
        if not reusable:
            return
        with self._session_results_lock:
            self._session_results[session_id] = reusable[-21:]
            self._session_results.move_to_end(session_id)
            while len(self._session_results) > self._max_session_results:
                self._session_results.popitem(last=False)


def build_default_runtime() -> V4Runtime:
    return V4Runtime(
        planner=V4Planner(planner_client()),
        executor=ParallelSourceExecutor(
            adapters=build_source_adapters(),
            per_tool_timeout_s=20.0,
            total_timeout_s=20.0,
        ),
        synthesizer=V4Synthesizer(synthesizer_client()),
    )


def _remaining(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _is_prior_result_reference(question: str) -> bool:
    normalized = " ".join(question.split()).casefold()
    return any(
        marker in normalized
        for marker in ("아까", "방금", "그 표", "몇 위랬", "그 중에")
    )


def _is_causal_followup(question: str) -> bool:
    normalized = " ".join(question.split()).casefold()
    return any(marker in normalized for marker in ("왜 ", "원인", "이유"))


def _prior_results_cover_answer_sources(
    answer_sources: tuple[str, ...],
    prior_results: tuple[SourceResult, ...],
) -> bool:
    available = {result.source for result in prior_results if result.status == "ok"}
    return bool(answer_sources) and set(answer_sources).issubset(available)


def _merge_results(
    previous: tuple[SourceResult, ...],
    current: tuple[SourceResult, ...],
) -> tuple[SourceResult, ...]:
    merged: dict[tuple[str, str], SourceResult] = {
        (result.source, result.query): result.model_copy(update={"cache_hit": True})
        for result in previous
    }
    for result in current:
        merged[(result.source, result.query)] = result
    return tuple(merged.values())


def _mark_citations_used(result: SourceResult) -> SourceResult:
    if result.status != "ok":
        return result
    return result.model_copy(
        update={
            "citations": tuple(
                citation.model_copy(
                    update={"source": _PUBLIC_SOURCE[result.source], "used": True}
                )
                for citation in result.citations
            )
        }
    )


def _execute_with_trace(executor: Any, plan: Any, **kwargs: Any) -> Any:
    detailed = getattr(executor, "execute_with_trace", None)
    if callable(detailed):
        return detailed(plan, **kwargs)
    started = time.monotonic()
    return SimpleNamespace(
        results=executor.execute(plan, **kwargs),
        trace={
            "elapsed_ms": (time.monotonic() - started) * 1000,
            "quorum_fired": None,
            "quorum_fire_ms": None,
            "tools": [],
        },
    )


def _empty_usage() -> dict[str, int | None]:
    return {"input_tokens": None, "output_tokens": None, "thinking_tokens": None}


def _normalized_synth_usage(trace: dict[str, Any]) -> dict[str, int | str | None]:
    usage = trace.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": _optional_int(usage.get("prompt_tokens")),
        "output_tokens": _optional_int(usage.get("completion_tokens")),
        "thinking_tokens": _optional_int(details.get("reasoning_tokens")),
        "finish_reason": str(trace["finish_reason"]) if trace.get("finish_reason") else None,
    }


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None
