from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
import inspect
import logging
import re
import threading
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.adapters import build_source_adapters
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    EvidenceEnvelope,
    SourceResult,
    V4Answer,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.gates import (
    apply_v4_gates,
    is_typed_absence_record,
)
from jw_chat_agent_poc.service.v4.llm import planner_client, synthesizer_client
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    LosslessInvariantError,
)
from jw_chat_agent_poc.service.v4.lossless_spine import (
    build_lossless_render,
    compose_lossless_answer,
    configured_lossless_mode,
    configured_requested_fields_mode,
    configured_request_satisfaction_mode,
    deterministic_fact_text,
)
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.session_state import SessionState, SessionStateStore
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome, V4Synthesizer
from jw_chat_agent_poc.service.v4.shadow import (
    CanonicalFact,
    build_canonical_ledger,
    build_grounding_shadow,
)
from jw_chat_agent_poc.service.v4.time_context import current_kst_date


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
_GROUNDING_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="v4-grounding-shadow",
)
_GROUNDING_SLOTS = threading.BoundedSemaphore(value=2)
ProgressCallback = Callable[[dict[str, Any]], None]
_PROGRESS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _release_grounding_slot(_future: Future[tuple[CanonicalFact, ...]]) -> None:
    _GROUNDING_SLOTS.release()


def _submit_grounding_ledger(
    results: Sequence[SourceResult],
) -> Future[tuple[CanonicalFact, ...]] | None:
    if not _GROUNDING_SLOTS.acquire(blocking=False):
        return None
    try:
        future = _GROUNDING_EXECUTOR.submit(build_canonical_ledger, tuple(results))
    except RuntimeError:
        _GROUNDING_SLOTS.release()
        return None
    future.add_done_callback(_release_grounding_slot)
    return future


class V4Runtime:
    def __init__(
        self,
        *,
        planner: V4Planner,
        executor: ParallelSourceExecutor,
        synthesizer: V4Synthesizer,
        state_store: SessionStateStore | Any | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._synthesizer = synthesizer
        self._state_store = state_store
        self._total_timeout_s = 150.0
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
        session_state = self._state_store.load(session_id) if self._state_store is not None else None
        plan_with_trace = getattr(self._planner, "plan_with_trace", None)
        if callable(plan_with_trace):
            planner_outcome = _call_with_state(
                plan_with_trace,
                question,
                selected_turns,
                budget_s=min(18.0, _remaining(deadline)),
                state=session_state,
            )
        else:
            planner_started = time.monotonic()
            planner_outcome = SimpleNamespace(
                plan=_call_with_state(
                    self._planner.plan,
                    question,
                    selected_turns,
                    budget_s=min(18.0, _remaining(deadline)),
                    state=session_state,
                ),
                trace={
                    "status": "unknown",
                    "elapsed_ms": (time.monotonic() - planner_started) * 1000,
                    "usage": _empty_usage(),
                },
            )
        plan = _bind_always_on_mart_query(planner_outcome.plan, question)
        plan = _bind_session_state_contract(plan, question, session_state)
        plan = _preserve_period_in_answer_queries(plan)
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
        source_queries = {
            source: _source_query_summary(queries)
            for source, queries in plan.tool_queries.items()
        }
        source_expected = {
            source: len(queries) for source, queries in plan.tool_queries.items()
        }
        source_results: dict[str, list[SourceResult]] = {
            source: [] for source in SOURCE_NAMES
        }
        for source in SOURCE_NAMES:
            _emit_source_progress(
                progress_callback,
                source=source,
                query=source_queries[source],
                results=(),
                expected=source_expected[source],
            )

        def source_completed(result_or_source: SourceResult | str) -> None:
            if isinstance(result_or_source, SourceResult):
                result = result_or_source
            else:
                source = str(result_or_source)
                if source not in source_queries:
                    return
                result = SourceResult(
                    source=source,
                    query=source_queries[source],
                    status="ok",
                )
            source_results[result.source].append(result)
            _emit_progress(
                progress_callback,
                "자료 수집",
                _source_progress_line(
                    result.source,
                    source_queries[result.source],
                    source_results[result.source],
                    source_expected[result.source],
                ),
                status=(
                    "done"
                    if len(source_results[result.source]) >= source_expected[result.source]
                    else "in_progress"
                ),
                raw_name=f"v4_source:{result.source}",
                raw_detail=source_queries[result.source],
            )

        prior_results = self._get_session_results(session_id)
        if not prior_results and session_state is not None:
            prior_results = _results_from_session_state(session_state)
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
            for result in first_execution.results:
                source_completed(result)
        else:
            first_execution = _execute_with_trace(
                self._executor,
                plan,
                session_id=session_id,
                total_timeout_s=min(50.0, _remaining(deadline)),
                answer_sources=plan.answer_sources,
                settle_sources=("mart",),
                soft_deadline_s=6.0,
                progress_callback=source_completed,
            )
            first_execution.trace.setdefault("session_result_reused", False)
        first_results = first_execution.results
        linked_plan = (
            _call_with_state(
                self._planner.link,
                plan,
                first_results,
                selected_turns,
                budget_s=min(7.0, _remaining(deadline)),
                state=session_state,
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
        absence_request = _absence_context_request(plan, first_results)
        absence_execution = SimpleNamespace(results=(), trace=None)
        if absence_request is not None:
            first_results = tuple(
                _tag_absence_confirmation(result, absence_request)
                for result in first_results
            )
        if (
            absence_request is not None
            and linked_plan is None
            and gap_request is None
            and _remaining(deadline) > 0.1
        ):
            tagged_first_results = tuple(
                _tag_absence_context(result, absence_request)
                if result.source == "web" and result.status == "ok"
                else result
                for result in first_results
            )
            reused_first_wave = any(
                result.source == "web"
                and result.status == "ok"
                and isinstance(result.payload, Mapping)
                and isinstance(result.payload.get("absence_context"), Mapping)
                for result in tagged_first_results
            )
            first_results = tagged_first_results
            if reused_first_wave:
                absence_execution = SimpleNamespace(
                    results=(),
                    trace={
                        "elapsed_ms": 0.0,
                        "tools": [],
                        "reused_first_wave": True,
                    },
                )
            else:
                _emit_progress(
                    progress_callback,
                    "관련 경과 조회",
                    "공식 문서 부재와 구분해 공개된 경과 자료를 조회합니다",
                )
                supplemental = _execute_with_trace(
                    self._executor,
                    _absence_context_plan(plan, absence_request),
                    session_id=session_id,
                    total_timeout_s=min(30.0, _remaining(deadline)),
                    answer_sources=("web",),
                    soft_deadline_s=4.0,
                    source_filter=("web",),
                    progress_callback=source_completed,
                )
                absence_execution = SimpleNamespace(
                    results=tuple(
                        _tag_absence_context(result, absence_request)
                        for result in supplemental.results
                    ),
                    trace={
                        **(supplemental.trace or {}),
                        "reused_first_wave": False,
                    },
                )
        current_results = (
            *first_results,
            *linked_results,
            *gap_execution.results,
            *absence_execution.results,
        )
        for source in SOURCE_NAMES:
            missing_results = max(
                1 if source_expected[source] == 0 else 0,
                source_expected[source] - len(source_results[source]),
            )
            for _ in range(missing_results):
                source_completed(
                    SourceResult(
                        source=source,
                        query=source_queries[source],
                        status="timeout",
                        notice="현재 답변 조회에서 미포함",
                    )
                )
        if prior_results and (
            _is_causal_followup(question)
            or (_is_prior_result_reference(question) and not can_reuse_prior)
        ):
            current_results = _merge_results(prior_results, current_results)
        results = tuple(_mark_citations_used(result) for result in current_results)
        self._remember_session_results(session_id, results)
        grounding_future = _submit_grounding_ledger(results)
        lossless_mode = configured_lossless_mode()
        requested_fields_mode = configured_requested_fields_mode()
        request_satisfaction_mode = configured_request_satisfaction_mode()
        lossless_error_type: str | None = None
        try:
            evidence_sets, deterministic_render = build_lossless_render(
                plan,
                results,
                observed_on=current_kst_date(),
            )
        except LosslessInvariantError:
            LOGGER.exception("v4 lossless spine invariant failed")
            raise
        except Exception as exc:  # noqa: BLE001 - legacy answer path must remain available
            LOGGER.exception("v4 lossless spine build failed")
            evidence_sets = ()
            deterministic_render = DeterministicRender(profile="market_analysis")
            lossless_error_type = type(exc).__name__
        visible_facts = deterministic_fact_text(
            deterministic_render,
            requested_fields_mode,
        )
        deterministic_facts = (
            visible_facts
            if lossless_mode == "inject"
            and deterministic_render.profile != "market_analysis"
            and visible_facts
            else None
        )
        _emit_progress(
            progress_callback,
            "답변 작성 중",
            "확인된 근거를 종합해 답변을 작성합니다",
        )
        synthesize_with_trace = getattr(self._synthesizer, "synthesize_with_trace", None)
        if callable(synthesize_with_trace):
            synthesis = _call_with_state(
                synthesize_with_trace,
                plan,
                results,
                selected_turns,
                budget_s=min(60.0, _remaining(deadline)),
                state=session_state,
                optional_kwargs={"deterministic_facts": deterministic_facts},
            )
        else:
            synthesis = SynthesisOutcome(
                text=_call_with_state(
                    self._synthesizer.synthesize,
                    plan,
                    results,
                    selected_turns,
                    budget_s=min(60.0, _remaining(deadline)),
                    state=session_state,
                    optional_kwargs={"deterministic_facts": deterministic_facts},
                ),
                trace={},
            )
        gated = apply_v4_gates(plan.resolved_question, synthesis.text, results)
        composition = compose_lossless_answer(
            deterministic_render,
            gated.text,
            synthesis_trace=synthesis.trace,
            mode=lossless_mode,
            requested_fields_mode=requested_fields_mode,
            request_satisfaction_mode=request_satisfaction_mode,
        )
        final_text = composition.text
        if grounding_future is None:
            grounding_shadow = {
                "mode": "SHADOW_RECORD_ONLY",
                "answer_mutation": False,
                "status": "unknown",
                "reason": "executor_saturated",
            }
        else:
            try:
                grounding_shadow = build_grounding_shadow(
                    final_text,
                    results,
                    ledger=grounding_future.result(timeout=2.0),
                )
            except Exception as exc:  # noqa: BLE001 - shadow must never change the answer path
                grounding_future.cancel()
                grounding_shadow = {
                    "mode": "SHADOW_RECORD_ONLY",
                    "answer_mutation": False,
                    "status": "unknown",
                    "error_type": type(exc).__name__,
                }
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
            "absence_context_elapsed_ms": (
                absence_execution.trace.get("elapsed_ms")
                if isinstance(absence_execution.trace, dict)
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
            "absence_context": {
                "triggered": absence_request is not None,
                "request": absence_request,
                "execution": absence_execution.trace,
            },
            "execution": {
                **first_execution.trace,
                "link_wave": linked_execution.trace,
            },
            "stage_timing": stage_timing,
            "gates": gated.trace,
            "lossless_spine": {
                **composition.trace,
                "build_error_type": lossless_error_type,
                "evidence_sets": [
                    {
                        "source": evidence_set.source,
                        "query_spec": list(evidence_set.query_spec),
                        "query_manifest": list(evidence_set.query_manifest),
                        "retrieved_at": evidence_set.retrieved_at,
                        "coverage": evidence_set.coverage.model_dump(mode="json"),
                        "item_failures": list(evidence_set.item_failures),
                        "source_refs": [
                            ref.model_dump(mode="json")
                            for ref in evidence_set.source_refs
                        ],
                    }
                    for evidence_set in evidence_sets
                ],
            },
            "typed_grounding_shadow": grounding_shadow,
        }
        source_names = [
                citation.source
                for result in results
                if result.status == "ok"
                for citation in result.citations
        ]
        source_names.extend(
            result.source
            for result in results
            if is_typed_absence_record(result)
        )
        sources = tuple(dict.fromkeys(source_names))
        next_state = _derive_session_state(
            question,
            plan,
            results,
            previous=session_state,
        )
        if self._state_store is not None:
            self._state_store.save(session_id, next_state)
        return V4Answer(
            text=final_text,
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


def _call_with_state(
    function: Callable[..., Any],
    *args: Any,
    state: SessionState | None,
    optional_kwargs: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    parameters = inspect.signature(function).parameters
    if "state" in parameters:
        kwargs["state"] = state
    accepts_arbitrary_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for key, value in (optional_kwargs or {}).items():
        if key in parameters or accepts_arbitrary_kwargs:
            kwargs[key] = value
    return function(*args, **kwargs)


def _derive_session_state(
    question: str,
    plan: Any,
    results: Sequence[SourceResult],
    *,
    previous: SessionState | None,
) -> SessionState:
    previous = previous or SessionState()
    entities = tuple(dict.fromkeys(_state_entities(results)))
    explicit_entities = tuple(
        entity for entity in entities if entity.casefold() in question.casefold()
    )
    topic_switched = bool(
        entities
        and previous.canonical_entities
        and set(entities).isdisjoint(previous.canonical_entities)
        and not _is_prior_result_reference(question)
    )
    retained_entities = () if topic_switched else previous.canonical_entities
    canonical_entities = tuple(dict.fromkeys((*retained_entities, *entities)))[:16]
    referenced = canonical_entities or previous.referenced_entity_set
    primary_entity = (
        explicit_entities[0]
        if explicit_entities
        else previous.primary_entity
        if previous.primary_entity and not topic_switched
        else entities[0]
        if entities
        else None
    )
    related_entities = tuple(
        entity for entity in canonical_entities if entity != primary_entity
    )
    numeric_facts = tuple(_state_numeric_facts(results))[:64]
    record_ids = tuple(dict.fromkeys(_state_record_ids(results)))[:64]
    return SessionState(
        canonical_entities=canonical_entities,
        primary_entity=primary_entity,
        mentioned_related_entities=related_entities,
        record_type=_record_type(question) or (None if topic_switched else previous.record_type),
        status_filter=_status_filter(question)
        or (() if topic_switched else previous.status_filter),
        country_filter=_country_filter(question)
        or (() if topic_switched else previous.country_filter),
        requested_grain=_requested_grain(question) or previous.requested_grain,
        referenced_entity_set=referenced,
        active_filters=_active_filters(question) or (() if topic_switched else previous.active_filters),
        time_window=_time_window(question) or (() if topic_switched else previous.time_window),
        comparison_anchor=primary_entity or previous.comparison_anchor,
        last_numeric_facts=numeric_facts or (() if topic_switched else previous.last_numeric_facts),
        last_source_record_ids=record_ids or (() if topic_switched else previous.last_source_record_ids),
    )


def _results_from_session_state(state: SessionState) -> tuple[SourceResult, ...]:
    facts_by_source: dict[str, list[dict[str, Any]]] = {}
    for fact in state.last_numeric_facts:
        source = str(fact.get("source") or "").strip()
        if source not in SOURCE_NAMES:
            continue
        path = str(fact.get("path") or "").strip()
        value = fact.get("value")
        is_number = not isinstance(value, bool) and isinstance(value, (int, float))
        is_date = fact.get("value_type") == "date" and _is_reusable_date_fact(path, value)
        if not path or not (is_number or is_date):
            continue
        restored = {"source": source, "path": path, "value": value}
        if is_date:
            restored.update(
                {
                    "column": str(fact.get("column") or path.rsplit(".", 1)[-1]),
                    "row_path": str(fact.get("row_path") or ""),
                    "value_type": "date",
                }
            )
        facts_by_source.setdefault(source, []).append(restored)

    results: list[SourceResult] = []
    for source in SOURCE_NAMES:
        numeric_facts = facts_by_source.get(source)
        if not numeric_facts:
            continue
        results.append(
            SourceResult(
                source=source,
                query="session state reuse",
                status="ok",
                payload={
                    "session_state_reuse": True,
                    "anchor_brand": state.primary_entity or state.comparison_anchor,
                    "canonical_entities": state.canonical_entities,
                    "primary_entity": state.primary_entity,
                    "mentioned_related_entities": state.mentioned_related_entities,
                    "record_type": state.record_type,
                    "status_filter": state.status_filter,
                    "country_filter": state.country_filter,
                    "referenced_entity_set": state.referenced_entity_set,
                    "active_filters": state.active_filters,
                    "time_window": state.time_window,
                    "last_numeric_facts": tuple(numeric_facts),
                    "last_source_record_ids": state.last_source_record_ids,
                },
                cache_hit=True,
            )
        )
    return tuple(results)


def _state_entities(results: Sequence[SourceResult]) -> list[str]:
    entities: list[str] = []
    entity_keys = {"brand", "anchor", "anchor_brand", "product", "product_name", "item_name"}
    for result in results:
        for path, value in _walk_state_values(result.payload):
            if path.rsplit(".", 1)[-1].split("[")[0] not in entity_keys:
                continue
            text = str(value or "").strip()
            if text:
                entities.append(text)
    return entities


def _state_numeric_facts(results: Sequence[SourceResult]) -> list[dict[str, Any]]:
    date_facts: list[dict[str, Any]] = []
    numeric_facts: list[dict[str, Any]] = []
    for result in results:
        if result.status != "ok":
            continue
        for path, value in _walk_state_values(result.payload):
            column = path.rsplit(".", 1)[-1].split("[", 1)[0]
            row_path = path.rsplit(".", 1)[0] if "." in path else ""
            if _is_reusable_date_fact(path, value):
                date_facts.append(
                    {
                        "source": result.source,
                        "path": path,
                        "column": column,
                        "row_path": row_path,
                        "value": value,
                        "value_type": "date",
                    }
                )
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            numeric_facts.append(
                {
                    "source": result.source,
                    "path": path,
                    "column": column,
                    "row_path": row_path,
                    "value": value,
                }
            )
    return [*date_facts, *numeric_facts][:64]


_REUSABLE_DATE_COLUMNS = {"reexam_date", "reexamination_date"}
_REUSABLE_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:\s*~\s*\d{4}-\d{2}-\d{2})?"
)


def _is_reusable_date_fact(path: str, value: Any) -> bool:
    column = path.rsplit(".", 1)[-1].split("[", 1)[0].casefold()
    return bool(
        column in _REUSABLE_DATE_COLUMNS
        and isinstance(value, str)
        and _REUSABLE_DATE_RE.fullmatch(value.strip())
    )


def _state_record_ids(results: Sequence[SourceResult]) -> list[str]:
    record_ids: list[str] = []
    for result in results:
        for path, value in _walk_state_values(result.payload):
            key = path.rsplit(".", 1)[-1].casefold()
            if key not in {"query_result_id", "record_id", "study_id", "item_seq"}:
                continue
            text = str(value or "").strip()
            if text:
                record_ids.append(text)
    return record_ids


def _walk_state_values(value: Any, prefix: str = ""):
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_state_values(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_state_values(item, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _requested_grain(question: str) -> str | None:
    normalized = question.casefold()
    if "진료과" in normalized:
        return "specialty"
    if "채널" in normalized:
        return "channel"
    if "시장" in normalized:
        return "market"
    if any(marker in normalized for marker in ("브랜드", "제품", "그 중", "아까")):
        return "brand"
    return None


def _active_filters(question: str) -> tuple[str, ...]:
    normalized = question.casefold()
    return tuple(
        marker
        for marker in ("국내", "진행 중", "모집 중", "완료", "급여", "허가")
        if marker in normalized
    )


def _record_type(question: str) -> str | None:
    normalized = question.casefold()
    if any(marker in normalized for marker in ("임상시험", "임상 시험", "임상")):
        return "clinical_trial"
    if any(marker in normalized for marker in ("특허", "재심사")):
        return "patent"
    if "급여" in normalized:
        return "reimbursement"
    if "허가" in normalized:
        return "approval"
    if "환자" in normalized:
        return "patient_count"
    if any(marker in normalized for marker in ("부작용", "안전성")):
        return "safety"
    if any(marker in normalized for marker in ("효능", "효과", "용법", "용량")):
        return "label"
    if any(marker in normalized for marker in ("매출", "점유율", "순위")):
        return "market_metric"
    return None


def _status_filter(question: str) -> tuple[str, ...]:
    normalized = question.casefold()
    values: list[str] = []
    if any(marker in normalized for marker in ("진행 중", "진행중", "모집 중", "모집중")):
        values.append("active")
    if "완료" in normalized:
        values.append("completed")
    return tuple(values)


def _country_filter(question: str) -> tuple[str, ...]:
    normalized = question.casefold()
    values: list[str] = []
    if any(marker in normalized for marker in ("국내", "한국")):
        values.append("KR")
    if any(marker in normalized for marker in ("미국", "미 FDA", "fda")):
        values.append("US")
    if any(marker in normalized for marker in ("글로벌", "전세계", "전 세계")):
        values.append("GLOBAL")
    return tuple(values)


def _time_window(question: str) -> tuple[str, ...]:
    periods = re.findall(r"20\d{2}(?:-(?:0[1-9]|1[0-2]))?", question)
    recent = re.search(r"최근\s*(\d{1,2})\s*년", question)
    if recent:
        periods.append(f"recent_{recent.group(1)}y")
    return tuple(dict.fromkeys(periods))


def build_default_runtime() -> V4Runtime:
    return V4Runtime(
        planner=V4Planner(planner_client()),
        executor=ParallelSourceExecutor(
            adapters=build_source_adapters(),
            per_tool_timeout_s=45.0,
            total_timeout_s=50.0,
        ),
        synthesizer=V4Synthesizer(synthesizer_client()),
        state_store=SessionStateStore.from_env(),
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


def _emit_progress(
    callback: ProgressCallback | None,
    name: str,
    detail: str,
    **metadata: Any,
) -> None:
    if callback is None:
        return
    try:
        callback({"name": name, "detail": detail, **metadata})
    except Exception:  # Progress is observational and must not become an answer gate.
        LOGGER.exception("V4 progress event emission failed")


def _expanded_intents_detail(intents: Sequence[str]) -> str:
    visible = [value.strip() for value in intents if value.strip()][:5]
    detail = "\n".join(f"- {value}" for value in visible)
    if not detail:
        return "질문에 맞는 조회 경로를 구성했습니다"
    remaining = max(0, len(intents) - len(visible))
    return f"{detail}\n- 외 {remaining}개" if remaining else detail


def _emit_source_progress(
    callback: ProgressCallback | None,
    *,
    source: str,
    query: str,
    results: Sequence[SourceResult],
    expected: int,
) -> None:
    _emit_progress(
        callback,
        "자료 수집",
        _source_progress_line(source, query, results, expected),
        status="started" if not results else "in_progress",
        raw_name=f"v4_source:{source}",
        raw_detail=query,
    )


def _source_progress_line(
    source: str,
    query: str,
    results: Sequence[SourceResult],
    expected: int,
) -> str:
    label = _PUBLIC_PROGRESS_SOURCE[source]
    quoted_query = f'"{query}"'
    if not results:
        return f"○ {label} {quoted_query} 조회 중"
    if len(results) < expected:
        return f"○ {label} {quoted_query} 조회 중 ({len(results)}/{expected})"

    elapsed_seconds = max((result.elapsed_ms for result in results), default=0.0) / 1000
    statuses = {result.status for result in results}
    if "ok" in statuses:
        return f"✓ {label} {quoted_query} 완료({elapsed_seconds:.2f}초)"
    if statuses == {"empty"}:
        return f"– {label} {quoted_query} 결과 없음"
    reason = "응답 지연" if "timeout" in statuses else "조회 오류"
    return f"! {label} {quoted_query} 미포함({reason})"


def _source_query_summary(queries: Sequence[str]) -> str:
    first = _public_progress_query(queries[0]) if queries else "조회 내용 확인 불가"
    remaining = max(0, len(queries) - 1)
    suffix = f" 외 {remaining}건" if remaining else ""
    return _one_line(first + suffix, limit=100)


def _public_progress_query(value: str) -> str:
    query = " ".join(value.split())
    query = _PROGRESS_URL_RE.sub(_mask_internal_progress_url, query)
    query = re.sub(
        r"\b(?:mcp|code-serving|read-only)-[a-z0-9.-]+\b",
        "내부 조회 경로",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\bslot(?:[_ -]?id)?\s*[:=#-]?\s*\d+\b",
        "내부 조회 식별자",
        query,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b",
        "관련 데이터 조회",
        query,
        flags=re.IGNORECASE,
    )


def _mask_internal_progress_url(match: re.Match[str]) -> str:
    url = match.group(0)
    host = (urlparse(url).hostname or "").casefold()
    if (
        host.endswith(".svc")
        or ".svc." in host
        or host in {"localhost", "127.0.0.1"}
        or host.startswith(("mcp-", "code-serving-", "read-only"))
    ):
        return "내부 조회 경로"
    return url


def _one_line(value: str, limit: int = 120) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bind_always_on_mart_query(plan: Any, question: str) -> Any:
    """Keep the always-on mart package bound to the user's requested entity."""

    mart_query = question.strip() or plan.resolved_question
    if _is_prior_result_reference(question):
        mart_query = plan.resolved_question
    queries = plan.tool_queries.model_copy(update={"mart": (mart_query,)})
    return plan.model_copy(update={"tool_queries": queries})


_RECORD_TYPE_QUERY_LABELS = {
    "clinical_trial": "임상시험",
    "patent": "특허",
    "reimbursement": "급여기준",
    "approval": "허가",
    "patient_count": "환자수",
    "market_metric": "시장 지표",
    "safety": "안전성",
    "label": "허가사항",
}
_STATUS_QUERY_LABELS = {
    "active": "진행 중",
    "completed": "완료",
}
_COUNTRY_QUERY_LABELS = {
    "KR": "국내",
    "US": "미국",
    "GLOBAL": "글로벌",
}
_SUBJECT_BEFORE_INTENT_RE = re.compile(
    r"^\s*(?P<subject>[0-9A-Za-z가-힣+_.-]{2,40})(?:의|은|는|이|가)?\s+"
    r"(?:급여|재심사|특허|허가|임상|환자|매출|점유율|순위|효능|부작용)"
)


def _bind_session_state_contract(
    plan: Any,
    question: str,
    state: SessionState | None,
) -> Any:
    """Bind omitted follow-up slots without carrying them onto a new subject."""

    if state is None or not _should_inherit_session_contract(question, state):
        return plan
    constraints = _session_query_constraints(state, question)
    if not constraints:
        return plan
    resolved_question = _append_missing_constraints(plan.resolved_question, constraints)
    updates = {
        source: tuple(
            _append_missing_constraints(query, constraints)
            for query in source_queries
        )
        for source, source_queries in plan.tool_queries.items()
    }
    return plan.model_copy(
        update={
            "resolved_question": resolved_question,
            "tool_queries": plan.tool_queries.model_copy(update=updates),
        }
    )


def _should_inherit_session_contract(question: str, state: SessionState) -> bool:
    normalized = " ".join(question.split()).casefold()
    if not normalized:
        return False
    if _is_prior_result_reference(question):
        return True
    subject_match = _SUBJECT_BEFORE_INTENT_RE.match(question)
    if subject_match is not None:
        subject = subject_match.group("subject").casefold()
        if subject not in {"그", "그중", "해당", "이번", "최근"}:
            primary = (state.primary_entity or state.comparison_anchor or "").casefold()
            return bool(primary and subject == primary)
    known_entities = tuple(
        dict.fromkeys(
            entity
            for entity in (
                state.primary_entity,
                *state.canonical_entities,
                *state.mentioned_related_entities,
            )
            if entity
        )
    )
    if any(entity.casefold() in normalized for entity in known_entities):
        return True
    return any(
        marker in normalized
        for marker in (
            "그 중",
            "그중",
            "아까",
            "방금",
            "왜",
            "재심사 언제",
            "언제 끝",
            "결과 알려",
            "그 결과",
            "그 자료",
        )
    )


def _session_query_constraints(
    state: SessionState,
    question: str,
) -> tuple[str, ...]:
    values: list[str] = []
    primary = state.primary_entity or state.comparison_anchor
    if primary:
        values.append(primary)
    requested_record_type = _record_type(question)
    inherit_record_scope = (
        requested_record_type is None or requested_record_type == state.record_type
    )
    if requested_record_type is None and state.record_type in _RECORD_TYPE_QUERY_LABELS:
        values.append(_RECORD_TYPE_QUERY_LABELS[state.record_type])
    if inherit_record_scope:
        values.extend(
            _STATUS_QUERY_LABELS[value]
            for value in state.status_filter
            if value in _STATUS_QUERY_LABELS
        )
        values.extend(
            _COUNTRY_QUERY_LABELS[value]
            for value in state.country_filter
            if value in _COUNTRY_QUERY_LABELS
        )
        values.extend(_public_period_label(value) for value in state.time_window)
    return tuple(dict.fromkeys(value for value in values if value))


def _public_period_label(value: str) -> str:
    recent = re.fullmatch(r"recent_(\d{1,2})y", value)
    if recent:
        return f"최근 {recent.group(1)}년"
    if re.fullmatch(r"20\d{2}", value):
        return f"{value}년"
    return value


def _append_missing_constraints(value: str, constraints: Sequence[str]) -> str:
    text = " ".join(value.split())
    normalized = text.casefold()
    missing = [item for item in constraints if item.casefold() not in normalized]
    return " ".join((text, *missing)) if missing else text


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


def _absence_context_request(
    plan: Any,
    results: Sequence[SourceResult],
) -> dict[str, Any] | None:
    lowered = plan.resolved_question.casefold()
    requested: tuple[str, str] | None = None
    if "급여" in lowered:
        requested = ("hira", "reimbursement")
    elif "허가" in lowered:
        requested = ("nedrug", "approval")
    if requested is None or requested[0] not in plan.answer_sources:
        return None
    official = tuple(result for result in results if result.source == requested[0])
    if not official:
        return None
    document_lookups = []
    for result in official:
        if not isinstance(result.payload, Mapping):
            continue
        lookup = result.payload.get("document_lookup")
        if (
            isinstance(lookup, Mapping)
            and lookup.get("document") == requested[1]
        ):
            document_lookups.append(lookup)
    absence_statuses = {
        str(lookup.get("outcome") or "") for lookup in document_lookups
    }
    if not document_lookups or not absence_statuses.issubset(
        {"doc_not_found", "confirmed_non_reimbursed"}
    ):
        return None
    if _has_official_document_reference(results, requested[0]):
        return None
    subject = str(document_lookups[0].get("subject") or "").strip()
    if not subject:
        return None
    return {
        "source": requested[0],
        "document": requested[1],
        "subject": subject,
        "absence_status": str(document_lookups[0].get("outcome") or ""),
        "query": plan.resolved_question,
    }


def _has_official_document_reference(
    results: Sequence[SourceResult],
    source: str,
) -> bool:
    official_hosts = {
        "hira": ("hira.or.kr",),
        "nedrug": ("nedrug.mfds.go.kr", "mfds.go.kr"),
    }.get(source, ())
    if not official_hosts:
        return False
    for result in results:
        if result.source == source or not isinstance(result.payload, Mapping):
            continue
        for path, value in _walk_state_values(result.payload):
            if path.rsplit(".", 1)[-1].casefold() not in {"url", "href", "link"}:
                continue
            host = (urlparse(str(value)).hostname or "").casefold()
            if any(host == expected or host.endswith(f".{expected}") for expected in official_hosts):
                return True
    return False


def _tag_absence_confirmation(
    result: SourceResult,
    request: Mapping[str, Any],
) -> SourceResult:
    if (
        result.status != "empty"
        or result.source != request.get("source")
        or not isinstance(result.payload, Mapping)
    ):
        return result
    lookup = result.payload.get("document_lookup")
    if not (
        isinstance(lookup, Mapping)
        and lookup.get("document") == request.get("document")
        and lookup.get("outcome")
        in {"doc_not_found", "coverage_unknown", "confirmed_non_reimbursed"}
        and lookup.get("subject") == request.get("subject")
    ):
        return result

    document = str(request["document"])
    subject = str(request["subject"])
    status = str(lookup["outcome"])
    absence_claims = (
        ("absence_confirmation", f"absence_confirmation:{document}")
        if status == "confirmed_non_reimbursed"
        else ()
    )
    claims = tuple(
        dict.fromkeys(
            (
                *(result.evidence.eligible_claims if result.evidence else ()),
                document,
                *absence_claims,
            )
        )
    )
    evidence = (
        result.evidence.model_copy(update={"eligible_claims": claims})
        if result.evidence is not None
        else EvidenceEnvelope(
            kind=result.source,
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=claims,
            causal=False,
            metric_type="document_absence",
            product=(subject,),
            subject_grain="brand",
        )
    )
    return result.model_copy(
        update={
            "payload": {
                **result.payload,
                "absence_confirmation": {
                    "source": result.source,
                    "doc_type": document,
                    "status": status,
                    "subject": subject,
                },
            },
            "evidence": evidence,
        }
    )


def _tag_absence_context(
    result: SourceResult,
    request: Mapping[str, Any],
) -> SourceResult:
    payload = result.payload if isinstance(result.payload, Mapping) else {"value": result.payload}
    filtered, usable = _absence_context_payload(payload, request)
    return result.model_copy(
        update={
            "status": result.status if usable else "empty",
            "payload": {
                **filtered,
                "absence_context": {
                    "source": request["source"],
                    "document": request["document"],
                    "subject": request["subject"],
                    "official_document_not_found": True,
                    "absence_status": str(
                        request.get("absence_status") or "doc_not_found"
                    ),
                    "reported_context_only": True,
                    "separate_from_official_fact": True,
                },
            },
        }
    )


def _absence_context_plan(plan: Any, request: Mapping[str, Any]) -> Any:
    subject = str(request["subject"])
    document_terms = {
        "reimbursement": "급여 약가 협상 결렬 재신청",
        "approval": "허가 심사 반려 재신청",
    }.get(str(request["document"]), "공식 문서 경과")
    query = f"{subject} {document_terms} 보도"
    queries = plan.tool_queries.model_copy(update={"web": (query,)})
    return plan.model_copy(
        update={
            "tool_queries": queries,
            "answer_sources": ("web",),
            "needs_second_hop": False,
            "linking_plan": "typed official absence context via one web query",
        }
    )


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
    direct_items = payload.get("items")
    if isinstance(direct_items, list):
        kept_items = _trusted_web_items(direct_items)
        return {**payload, "items": kept_items}, bool(kept_items)
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
            kept_items = _trusted_web_items(render_data["items"])
            copied_call["render_data"] = {**render_data, "items": kept_items}
            usable = usable or bool(kept_items)
        kept_calls.append(copied_call)
    return {**payload, "calls": kept_calls}, usable


def _absence_context_payload(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    trusted_payload, _ = _official_gap_payload(payload)
    direct_items = trusted_payload.get("items")
    if isinstance(direct_items, list):
        kept_items = _relevant_absence_context_items(direct_items, request)
        return {**trusted_payload, "items": kept_items}, bool(kept_items)

    calls = trusted_payload.get("calls")
    if not isinstance(calls, list):
        return trusted_payload, False
    kept_calls: list[Any] = []
    usable = False
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        copied_call = dict(call)
        render_data = call.get("render_data")
        if isinstance(render_data, Mapping) and isinstance(render_data.get("items"), list):
            kept_items = _relevant_absence_context_items(render_data["items"], request)
            copied_call["render_data"] = {**render_data, "items": kept_items}
            usable = usable or bool(kept_items)
        kept_calls.append(copied_call)
    return {**trusted_payload, "calls": kept_calls}, usable


def _relevant_absence_context_items(
    items: Sequence[Any],
    request: Mapping[str, Any],
) -> list[dict[str, Any]]:
    subject_markers = _absence_subject_markers(request)
    event_markers = {
        "reimbursement": ("협상", "결렬", "재신청", "약가"),
        "approval": ("허가", "심사", "반려", "재신청"),
    }.get(str(request.get("document") or ""), ())
    kept_items: list[dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        searchable = " ".join(
            str(raw_item.get(key) or "")
            for key in ("title", "snippet", "description", "content")
        )
        normalized = _normalize_context_text(searchable)
        if not any(marker and marker in normalized for marker in subject_markers):
            continue
        if not any(_normalize_context_text(marker) in normalized for marker in event_markers):
            continue
        if not _has_observed_publication_date(raw_item):
            continue
        kept_items.append(dict(raw_item))
    return kept_items


def _absence_subject_markers(request: Mapping[str, Any]) -> tuple[str, ...]:
    markers = [_normalize_context_text(request.get("subject"))]
    query = str(request.get("query") or "")
    for group in re.findall(r"\(([^()]*)\)", query):
        markers.extend(
            _normalize_context_text(token)
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+.-]{2,}", group)
        )
    return tuple(dict.fromkeys(marker for marker in markers if marker))


def _normalize_context_text(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _has_observed_publication_date(item: Mapping[str, Any]) -> bool:
    return any(
        re.match(
            r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:\D|$)",
            str(item.get(key) or "").strip(),
        )
        for key in ("published_at", "published_date", "date")
    )


def _trusted_web_items(items: Sequence[Any]) -> list[dict[str, Any]]:
    kept_items: list[dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        tier = _official_web_tier(str(raw_item.get("url") or ""))
        if tier is None:
            continue
        kept_items.append({**raw_item, "trust_tier": tier})
    return kept_items


def _official_web_tier(url: str) -> str | None:
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith(".go.kr") or host in {"korea.kr", "hira.or.kr", "www.hira.or.kr"}:
        return "TIER1"
    if host.endswith(".ac.kr") or host.endswith(".or.kr") or host in {"yna.co.kr", "www.yna.co.kr"}:
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
