from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
import logging
import re
import threading
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
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
_PUBLIC_PROGRESS_SOURCE = {
    **_PUBLIC_SOURCE,
    "hira": "건강보험심사평가원",
    "nedrug": "식품의약품안전처",
}
LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, str]], None]


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
        self._total_timeout_s = 96.0
        self._session_results: OrderedDict[str, tuple[SourceResult, ...]] = OrderedDict()
        self._session_results_lock = threading.Lock()
        self._max_session_results = 1024

    def answer(
        self,
        question: str,
        *,
        conversation_id: str | None,
        turns: Sequence[ConversationTurn],
        progress_callback: ProgressCallback | None = None,
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
        plan = _preserve_period_in_answer_queries(planner_outcome.plan)
        _emit_progress(
            progress_callback,
            "질문 해석",
            _one_line(plan.resolved_question),
        )
        _emit_progress(
            progress_callback,
            "조회 계획",
            _expanded_intents_detail(plan.expanded_intents),
        )
        completed_sources: list[str] = []

        def source_completed(source: str) -> None:
            public = _PUBLIC_PROGRESS_SOURCE.get(source, "조회 자료")
            if public in completed_sources:
                return
            completed_sources.append(public)
            _emit_progress(
                progress_callback,
                "자료 수집 중",
                " · ".join(f"{name} 완료" for name in completed_sources),
            )

        prior_results = self._get_session_results(session_id)
        can_reuse_prior = (
            prior_results
            and _is_prior_result_reference(question)
            and (
                _prior_results_cover_answer_sources(plan.answer_sources, prior_results)
                or _prior_results_cover_filter_followup(
                    question,
                    plan.answer_sources,
                    prior_results,
                )
            )
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
                total_timeout_s=min(45.0, _remaining(deadline)),
                answer_sources=plan.answer_sources,
                soft_deadline_s=6.0,
                progress_callback=source_completed,
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
        if linked_plan is not None:
            _emit_progress(
                progress_callback,
                "연결 조회",
                "첫 조회에서 확인한 대상을 바탕으로 관련 자료를 한 번 더 조회합니다",
            )
        linked_execution = (
            _execute_with_trace(
                self._executor,
                linked_plan,
                session_id=session_id,
                total_timeout_s=min(30.0, _remaining(deadline)),
                answer_sources=linked_plan.answer_sources,
                soft_deadline_s=6.0,
                progress_callback=source_completed,
            )
            if linked_plan is not None and _remaining(deadline) > 0.1
            else SimpleNamespace(results=(), trace=None)
        )
        linked_results = linked_execution.results
        gap_request = _gap_fill_request(plan, first_results)
        gap_execution = SimpleNamespace(results=(), trace=None)
        if gap_request is not None and linked_plan is None and _remaining(deadline) > 0.1:
            _emit_progress(
                progress_callback,
                "연결 조회",
                "공식 자료의 누락 기간을 보완할 발표 자료를 별도로 조회합니다",
            )
            gap_plan = _gap_fill_plan(plan, gap_request)
            gap_execution = _execute_with_trace(
                self._executor,
                gap_plan,
                session_id=session_id,
                total_timeout_s=min(30.0, _remaining(deadline)),
                answer_sources=("web",),
                soft_deadline_s=4.0,
                source_filter=("web",),
                progress_callback=source_completed,
            )
            gap_execution = SimpleNamespace(
                results=tuple(_tag_gap_result(result, gap_request) for result in gap_execution.results),
                trace=gap_execution.trace,
            )
        current_results = (*first_results, *linked_results, *gap_execution.results)
        if prior_results and (
            _is_causal_followup(question)
            or (_is_prior_result_reference(question) and not can_reuse_prior)
        ):
            current_results = _merge_results(prior_results, current_results)
        results = tuple(_mark_citations_used(result) for result in current_results)
        self._remember_session_results(session_id, results)
        _emit_progress(
            progress_callback,
            "답변 작성 중",
            "확인된 근거를 종합해 답변을 작성합니다",
        )
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
        planner_usage = _normalized_planner_usage(planner_outcome.trace.get("usage"))
        stage_timing = {
            "planner_elapsed_ms": planner_outcome.trace.get("elapsed_ms"),
            "wave_elapsed_ms": first_execution.trace.get("elapsed_ms"),
            "link_wave_elapsed_ms": (
                linked_execution.trace.get("elapsed_ms")
                if isinstance(linked_execution.trace, dict)
                else None
            ),
            "synth_elapsed_ms": synthesis.trace.get("elapsed_ms"),
            "gap_fill_elapsed_ms": (
                gap_execution.trace.get("elapsed_ms")
                if isinstance(gap_execution.trace, dict)
                else "not_applicable"
            ),
            "total_elapsed_ms": elapsed_ms,
        }
        trace = {
            "v4": True,
            "planner_serving": planner_outcome.trace.get("serving_id", "not_applicable"),
            "planner_model": planner_outcome.trace.get("model", "not_applicable"),
            "synth_serving": synthesis.trace.get("serving_id", "not_applicable"),
            "synth_model": synthesis.trace.get("model", "not_applicable"),
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
            "gap_fill": _gap_fill_trace(gap_request, gap_execution),
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
            per_tool_timeout_s=30.0,
            total_timeout_s=45.0,
        ),
        synthesizer=V4Synthesizer(synthesizer_client()),
    )


def _remaining(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _is_prior_result_reference(question: str) -> bool:
    normalized = " ".join(question.split()).casefold()
    return any(
        marker in normalized
        for marker in ("아까", "방금", "그 표", "몇 위랬", "그 중")
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


def _prior_results_cover_filter_followup(
    question: str,
    answer_sources: tuple[str, ...],
    prior_results: tuple[SourceResult, ...],
) -> bool:
    normalized = " ".join(question.split()).casefold()
    is_filter = "그 중" in normalized and any(
        marker in normalized for marker in ("국내", "진행 중", "모집 중", "완료")
    )
    if not is_filter:
        return False
    available = {result.source for result in prior_results if result.status == "ok"}
    return bool(available.intersection(answer_sources))


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


def _empty_usage() -> dict[str, str]:
    return {
        "input_tokens": "not_applicable",
        "output_tokens": "not_applicable",
        "thinking_tokens": "not_applicable",
        "measurement": "not_applicable",
    }


def _normalized_synth_usage(trace: dict[str, Any]) -> dict[str, int | str]:
    usage = trace.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    if not usage:
        return {
            **_empty_usage(),
            "finish_reason": "not_applicable",
        }
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": _int_or_zero(usage.get("prompt_tokens")),
        "output_tokens": _int_or_zero(usage.get("completion_tokens")),
        "thinking_tokens": _int_or_zero(details.get("reasoning_tokens")),
        "finish_reason": str(trace.get("finish_reason") or "not_reported"),
        "measurement": "reported",
    }


def _int_or_zero(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _normalized_planner_usage(value: object) -> dict[str, int | str]:
    if not isinstance(value, Mapping) or not value:
        return _empty_usage()
    return {
        "input_tokens": _int_or_zero(value.get("input_tokens")),
        "output_tokens": _int_or_zero(value.get("output_tokens")),
        "thinking_tokens": _int_or_zero(value.get("thinking_tokens")),
        "measurement": "reported",
    }


def _emit_progress(callback: ProgressCallback | None, name: str, detail: str) -> None:
    if callback is None:
        return
    try:
        callback({"name": name, "detail": detail})
    except Exception:  # Progress is observational and must not become an answer gate.
        LOGGER.exception("V4 progress event emission failed")


def _expanded_intents_detail(intents: Sequence[str]) -> str:
    visible = [value.strip() for value in intents if value.strip()][:5]
    detail = " · ".join(visible) if visible else "질문에 맞는 조회 경로를 구성했습니다"
    remaining = max(0, len(intents) - len(visible))
    return f"{detail} · 외 {remaining}개" if remaining else detail


def _one_line(value: str, limit: int = 120) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _preserve_period_in_answer_queries(plan: Any) -> Any:
    match = re.search(r"최근\s*\d+\s*년", plan.resolved_question)
    if match is None:
        return plan
    period = " ".join(match.group(0).split())
    queries = plan.tool_queries
    updates: dict[str, tuple[str, ...]] = {}
    for source in ("hira", "nedrug"):
        if source not in plan.answer_sources:
            continue
        source_queries = getattr(queries, source)
        if any(re.search(r"최근\s*\d+\s*년", query) for query in source_queries):
            continue
        updates[source] = tuple(f"{query} {period}" for query in source_queries)
    if not updates:
        return plan
    return plan.model_copy(
        update={"tool_queries": queries.model_copy(update=updates)}
    )


def _gap_fill_request(
    plan: Any,
    results: Sequence[SourceResult],
) -> dict[str, Any] | None:
    for result in results:
        if result.source not in {"hira", "nedrug"} or result.source not in plan.answer_sources:
            continue
        if not isinstance(result.payload, Mapping):
            continue
        coverage = result.payload.get("period_coverage")
        if not isinstance(coverage, Mapping):
            continue
        periods = coverage.get("periods")
        if not isinstance(periods, list):
            continue
        missing = [
            str(item.get("period"))
            for item in periods
            if isinstance(item, Mapping)
            and str(item.get("status") or "").casefold() in {"error", "no_data"}
        ]
        if missing:
            return {
                "source": result.source,
                "missing_periods": missing,
                "query": result.query,
            }
    return None


def _gap_fill_plan(plan: Any, request: Mapping[str, Any]) -> Any:
    periods = " ".join(str(period) for period in request["missing_periods"])
    query = f"{request['query']} {periods} 공식 통계 발표 보도자료"
    queries = plan.tool_queries.model_copy(update={"web": (query,)})
    return plan.model_copy(
        update={
            "tool_queries": queries,
            "answer_sources": ("web",),
            "needs_second_hop": False,
            "linking_plan": "typed period gap fill via one web query",
        }
    )


def _tag_gap_result(result: SourceResult, request: Mapping[str, Any]) -> SourceResult:
    payload = result.payload if isinstance(result.payload, Mapping) else {"value": result.payload}
    filtered_payload, usable = _official_gap_payload(payload)
    return result.model_copy(
        update={
            "status": result.status if usable else "empty",
            "payload": {
                **filtered_payload,
                "gap_fill": {
                    "source": request["source"],
                    "missing_periods": list(request["missing_periods"]),
                    "separate_from_official_series": True,
                    "quantitative_tiers_allowed": ["TIER1", "TIER2"],
                },
            }
        }
    )


def _official_gap_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    calls = payload.get("calls")
    if not isinstance(calls, list):
        return dict(payload), False
    kept_calls: list[Any] = []
    usable = False
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        copied_call = dict(call)
        render_data = call.get("render_data")
        if isinstance(render_data, Mapping) and isinstance(render_data.get("items"), list):
            kept_items: list[dict[str, Any]] = []
            for raw_item in render_data["items"]:
                if not isinstance(raw_item, Mapping):
                    continue
                tier = _official_web_tier(str(raw_item.get("url") or ""))
                if tier is None:
                    continue
                kept_items.append({**raw_item, "trust_tier": tier})
            copied_call["render_data"] = {**render_data, "items": kept_items}
            usable = usable or bool(kept_items)
        kept_calls.append(copied_call)
    return {**payload, "calls": kept_calls}, usable


def _official_web_tier(url: str) -> str | None:
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith(".go.kr") or host in {"korea.kr", "hira.or.kr", "www.hira.or.kr"}:
        return "TIER1"
    if host.endswith(".ac.kr") or host.endswith(".or.kr"):
        return "TIER2"
    return None


def _gap_fill_trace(request: Mapping[str, Any] | None, execution: Any) -> dict[str, Any]:
    if request is None:
        return {"attempted": False, "reason": "no_period_gap"}
    return {
        "source": request["source"],
        "missing_periods": list(request["missing_periods"]),
        "attempted": bool(getattr(execution, "results", ())),
        "result_statuses": [result.status for result in getattr(execution, "results", ())],
    }
