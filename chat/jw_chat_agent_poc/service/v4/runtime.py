from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from jw_chat_agent_poc.service.answer_safety import scope_document_absence_claims
from jw_chat_agent_poc.service.context_scope import (
    explicit_file_comparison_sources,
    has_explicit_file_source_comparison,
    has_file_axis_reference,
)
from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.latency_instrumentation import (
    begin_latency_probe,
    begin_pre_answer_timeline,
    lane_spans_from_execution_trace,
)
from jw_chat_agent_poc.service.prior_turn_lane import (
    PRIOR_TURN_EVIDENCE_ID,
    allow_legacy_result_reuse,
    append_prior_turn_annotation,
    build_prior_turn_context,
    finalize_prior_turn_requery,
    merge_prior_turn_entities,
    prior_turn_evidence_reference,
)
from jw_chat_agent_poc.service.v4.adapters import build_source_adapters
from jw_chat_agent_poc.service.v4.axis_shaping import question_axes, shape_axis_queries
from jw_chat_agent_poc.service.v4.charts import (
    build_grounded_charts,
    chart_was_requested,
    requested_chart_absence_reason,
    requested_chart_metric,
)
from jw_chat_agent_poc.service.v4.claim_ir import classify_answer_claims
from jw_chat_agent_poc.service.v4.clinical_query_policy import clinical_scope_suffix
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    EvidenceEnvelope,
    PlannerOutput,
    QueryScope,
    SourceName,
    SourceResult,
    V4Answer,
)
from jw_chat_agent_poc.service.v4.document_lane import (
    file_lane_id,
    is_document_overview_question,
)
from jw_chat_agent_poc.service.v4.entity_registry import resolve_disease_entity
from jw_chat_agent_poc.service.v4.evidence_display import (
    build_evidence_display_catalog,
)
from jw_chat_agent_poc.service.v4.executor import (
    ParallelSourceExecutor,
    _result_exclusion_reason,
)
from jw_chat_agent_poc.service.v4.expansion import (
    _kcd_codes,
    build_second_hop_expansion,
    expand_parameter_axes,
)
from jw_chat_agent_poc.service.v4.fact_digest import (
    FactDigest,
    body_relevance_trace,
    build_fact_digest,
    fact_digest_contract_coverage,
    period_scope_trace,
    render_core_answer,
    render_file_analytics_tables,
    render_hira_statistics_tables,
)
from jw_chat_agent_poc.service.v4.fallback_routing import (
    augment_explicit_substance_queries,
    compose_all_source_queries,
    sanitize_planner_fallback,
)
from jw_chat_agent_poc.service.v4.gates import (
    apply_v4_gates,
    is_typed_absence_record,
)
from jw_chat_agent_poc.service.v4.insight_contract import (
    expand_s17_insight_from_digest,
    promote_context_to_s17_insight,
)
from jw_chat_agent_poc.service.v4.insight_lane import InsightLane, SectionReadyCallback
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.lane_execution import build_lane_execution_records
from jw_chat_agent_poc.service.v4.llm import planner_client, synthesizer_client
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    EvidenceSet,
    LosslessInvariantError,
)
from jw_chat_agent_poc.service.v4.lossless_spine import (
    align_core_answer_to_question,
    build_lossless_render,
    compose_lossless_answer,
    compose_streaming_body,
    configured_lossless_mode,
    configured_request_satisfaction_mode,
    configured_requested_fields_mode,
    deterministic_fact_text,
    ensure_s9_insight_surface,
    visible_s9_sources,
    visible_surface_axes,
)
from jw_chat_agent_poc.service.v4.narrative_realization import (
    measure_final_narrative_surface,
)
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.query_scope import (
    apply_source_call_cap,
    route_queries_by_grain,
)
from jw_chat_agent_poc.service.v4.retrieval_events import (
    provider_quota_notice,
    retrieval_event_from_result,
    utc_now,
)
from jw_chat_agent_poc.service.v4.scope_provenance import (
    build_scope_provenance_projection,
)
from jw_chat_agent_poc.service.v4.semantic_contract import (
    derive_answer_contract,
    merge_interpretation_contract,
)
from jw_chat_agent_poc.service.v4.semantic_realization import (
    SemanticEvidenceContext,
    ensure_core_answer_surface,
    evidence_has_hira_patient_count,
    evidence_has_temporal_support,
    evidence_hira_code_count,
    evidence_support_text,
    evidence_temporal_support_texts,
    realize_semantic_surface,
    strip_s17_body_metadata,
)
from jw_chat_agent_poc.service.v4.session_state import SessionState, SessionStateStore
from jw_chat_agent_poc.service.v4.shadow import (
    CanonicalFact,
    build_canonical_ledger,
    build_grounding_shadow,
)
from jw_chat_agent_poc.service.v4.source_labels import (
    SOURCE_LABELS as _PUBLIC_SOURCE,
)
from jw_chat_agent_poc.service.v4.source_tiers import (
    entity_completion_rows,
    fan_out_tier_zero_queries,
    render_axis_tokens,
    sanitize_planner_entities,
    tier_funnel,
)
from jw_chat_agent_poc.service.v4.surface_binding import sanitize_bound_surface
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    SynthesisPolicy,
    limit_evidence_sets_for_render,
    prune_unsupported_source_queries,
)
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome, V4Synthesizer
from jw_chat_agent_poc.service.v4.time_context import current_kst_date

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
AnswerReadyCallback = Callable[[str, str, tuple[str, ...]], None]
_PROGRESS_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


_SCOPE_NOTICE_LABELS = {
    "mart": "시장 데이터",
    "nedrug": "의약품 정보",
    "hira": "HIRA",
    "openfda": "FDA",
    "clinicaltrials": "임상시험",
    "web": "웹 검색",
    "patent": "특허",
}
# Invariant 1: the surface must separate "the source answered nothing" from
# "we never got the answer back". Both used to read as absence of data. The
# wording is keyed off the exclusion reasons the executor already types, so no
# second classification exists to drift away from the trace.
_SHORTFALL_PHRASING: tuple[tuple[str, str], ...] = (
    ("upstream_timeout", "조회 시간이 초과되어 이번 답변에 반영되지 않았습니다"),
    ("empty_result", "조회했으나 해당하는 자료를 찾지 못했습니다"),
    ("provider_quota", "조회 한도에 걸려 이번 답변에 반영되지 않았습니다"),
    ("parse_error", "응답을 해석하지 못해 이번 답변에 반영되지 않았습니다"),
    ("scope_limit", "조회 범위 제한으로 이번 답변에 반영되지 않았습니다"),
    ("upstream_error", "조회에 실패해 이번 답변에 반영되지 않았습니다"),
)


_RETRIEVAL_FAILURE_CORE_COPY = {
    "FILE_SQL_QUERY_FAILED": (
        "업로드 파일 집계 실행이 실패해 요청하신 값을 확인하지 못했습니다."
    ),
    "FILE_SQL_QUERY_TIMEOUT": (
        "업로드 파일 집계 조회가 응답 시간 초과로 종료되어 "
        "요청하신 값을 확인하지 못했습니다."
    ),
}


def _source_failure_core(results: Sequence[SourceResult]) -> str | None:
    return next(
        (
            _RETRIEVAL_FAILURE_CORE_COPY[result.failure_reason]
            for result in results
            if result.failure_reason in _RETRIEVAL_FAILURE_CORE_COPY
        ),
        None,
    )


def _retrieval_shortfall_notice(results: Sequence[Any]) -> str | None:
    """Name the calls that ran but brought nothing back.

    ``_query_scope_notice`` only speaks about queries that were never issued,
    so a lane whose every call timed out reads to the user exactly like a lane
    with no data. This walks the executed results instead and reports, per
    lane, how many calls produced evidence and why the rest did not.
    """
    executed: dict[str, list[Any]] = {}
    for result in results:
        source = getattr(result, "source", None)
        if source is None:
            continue
        executed.setdefault(str(source), []).append(result)

    notices: list[str] = []
    for source, lane_results in executed.items():
        shortfalls: dict[str, list[str]] = {}
        quota_providers: list[str] = []
        for result in lane_results:
            reason = _result_exclusion_reason(result)
            detail = getattr(result, "failure_detail", None) or {}
            if isinstance(detail, Mapping):
                quota_providers.extend(
                    str(item)
                    for item in (detail.get("provider_quotas") or [])
                    if item
                )
            if reason is None:
                continue
            shortfalls.setdefault(reason, []).append(str(getattr(result, "query", "")))
        label = _SCOPE_NOTICE_LABELS.get(source, source)
        # A call that kept part of its work is neither a success nor a failure;
        # without this the preserved-versus-dropped split would never be said
        # out loud, because the result still carries status "ok".
        partial_lines: list[str] = []
        nested_summary_reported = False
        for result in lane_results:
            detail = getattr(result, "failure_detail", None) or {}
            if getattr(result, "status", None) == "ok" and isinstance(detail, Mapping):
                result_nested_reported = False
                nested_total = int(detail.get("nested_call_count") or 0)
                nested_failures = detail.get("nested_failure_counts") or {}
                nested_failed = (
                    sum(int(value or 0) for value in nested_failures.values())
                    if isinstance(nested_failures, Mapping)
                    else 0
                )
                if nested_total > 0 and nested_failed > 0:
                    nested_summary_reported = True
                    result_nested_reported = True
                    partial_lines.append(
                        f"{label} 조회 {nested_total}건 중 "
                        f"{max(0, nested_total - nested_failed)}건에서 자료를 확보했습니다."
                    )
                    quota_counts = detail.get("provider_quota_counts") or {}
                    if isinstance(quota_counts, Mapping):
                        for provider, count in quota_counts.items():
                            partial_lines.append(
                                f"- {int(count or 0)}건: {provider_quota_notice(str(provider))}"
                            )
                providers = detail.get("provider_quotas") or []
                if not result_nested_reported:
                    for provider in dict.fromkeys(str(item) for item in providers if item):
                        partial_lines.append(f"- {provider_quota_notice(provider)}")
            partial = (getattr(result, "failure_detail", None) or {}).get(
                "partial_preservation"
            )
            if not isinstance(partial, Mapping):
                continue
            dropped = int(partial.get("dropped") or 0)
            if dropped <= 0:
                continue
            partial_lines.append(
                f"- 그중 1건은 대상 {int(partial.get('requested') or 0)}개 중 "
                f"{int(partial.get('preserved') or 0)}개까지만 조회했고, "
                f"나머지 {dropped}개는 조회 시간이 초과되어 반영되지 않았습니다"
            )
        if not shortfalls and not partial_lines:
            continue
        if not shortfalls and nested_summary_reported:
            notices.extend(partial_lines)
            continue
        total = len(lane_results)
        grounded = total - sum(len(queries) for queries in shortfalls.values())
        notices.append(
            f"{label} 조회 {total}건 중 {grounded}건에서 자료를 확보했습니다."
        )
        known = {reason for reason, _phrase in _SHORTFALL_PHRASING}
        ordered = [
            (reason, phrase)
            for reason, phrase in _SHORTFALL_PHRASING
            if reason in shortfalls
        ]
        # A reason the phrasing table has not caught up with is still reported,
        # never dropped, so an unnamed failure cannot masquerade as success.
        ordered.extend(
            (reason, "이번 답변에 반영되지 않았습니다")
            for reason in sorted(shortfalls)
            if reason not in known
        )
        for reason, phrase in ordered:
            queries = shortfalls[reason]
            if reason == "provider_quota":
                providers = tuple(dict.fromkeys(quota_providers)) or (source,)
                for provider in providers:
                    notices.append(
                        f"- {len(queries)}건: {provider_quota_notice(provider, label=label)}"
                    )
                continue
            notices.append(f"- {len(queries)}건은 {phrase}")
        notices.extend(partial_lines)
    return "\n".join(notices) if notices else None


_PLANNER_FALLBACK_NOTICE = (
    "질문 해석이 시간 내 완료되지 않아 축소된 범위로 조회했습니다. "
    "이 답변은 제한된 조회 범위를 기준으로 확인해 주세요."
)
_PLANNER_PARTIAL_NOTICE = (
    "질문 해석 응답이 완전히 종료되기 전에 확인된 조회 계획을 사용했습니다. "
    "일부 확장 정보가 포함되지 않았을 수 있습니다."
)


def _planner_degradation_notice(trace: Mapping[str, Any]) -> str | None:
    status = str(trace.get("status") or "")
    if status == "fallback":
        return _PLANNER_FALLBACK_NOTICE
    if status == "partial_recovered":
        return _PLANNER_PARTIAL_NOTICE
    return None


def _query_scope_notice(plan: Any) -> str | None:
    scope = getattr(plan, "query_scope", None)
    if scope is None:
        return None
    labels = _SCOPE_NOTICE_LABELS
    notices: list[str] = []
    for source, omitted in scope.omitted_queries.items():
        omitted_count = len(omitted)
        if omitted_count == 0:
            continue
        requested = int(scope.requested_calls.get(source, 0))
        executed = int(scope.executed_calls.get(source, 0))
        notices.append(
            f"{labels.get(source, source)} 조회는 요청 {requested}건 중 "
            f"{executed}건을 실행했습니다. 나머지 {omitted_count}건은 "
            "이번 답변의 조회 상한으로 제외했습니다."
        )
    return "\n".join(notices) if notices else None


_AXIS_FOLLOWUP_LABELS = {
    "연령": "연령별",
    "성별": "성별",
    "기관종별": "기관종별",
    "요양기관종별": "요양기관종별",
    "월별": "월별",
    "연도별": "연도별",
    "입원/외래": "입원/외래별",
    "입원 외래": "입원/외래별",
}


def _axis_followup_label(question: str) -> str | None:
    normalized = " ".join(question.split())
    for marker, label in _AXIS_FOLLOWUP_LABELS.items():
        if marker in normalized:
            return label
    return None


def _session_inheritance_notice(
    question: str,
    state: SessionState | None,
) -> str | None:
    axis_label = _axis_followup_label(question)
    if state is None or axis_label is None or not _should_inherit_session_contract(question, state):
        return None
    entities = tuple(
        dict.fromkeys(
            (
                *state.referenced_entity_set,
                *state.canonical_entities,
                state.primary_entity or "",
            )
        )
    )
    inherited = tuple(value for value in entities if value) + tuple(
        _public_period_label(value) for value in state.time_window if value
    )
    if not inherited:
        return None
    return f"앞선 질문의 {' · '.join(inherited)} 기준으로 {axis_label}을 조회했습니다."


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


def _chart_record_ids(
    evidence_sets: Sequence[EvidenceSet],
    rendered_record_ids: Sequence[str],
    *,
    question: str,
    per_source_limit: int,
) -> tuple[str, ...]:
    if requested_chart_metric(question) is None:
        return tuple(rendered_record_ids)
    selected_sets, _trace = limit_evidence_sets_for_render(
        evidence_sets,
        per_source_limit=per_source_limit,
        question=question,
    )
    return tuple(
        dict.fromkeys(
            (
                *rendered_record_ids,
                *(
                    record.evidence_id
                    for evidence_set in selected_sets
                    for record in evidence_set.records
                ),
            )
        )
    )


def _answer_surface_results(
    question: str,
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    *,
    followup: bool = False,
) -> tuple[tuple[SourceResult, ...], dict[str, Any]]:
    """Keep every received lane on the answer surface; only order the primary lane first."""

    if has_explicit_file_source_comparison(question):
        comparison_sources = explicit_file_comparison_sources(question)
        allowed_sources = frozenset(("document", *comparison_sources))
        axis = "file_" + "_".join(comparison_sources)
    elif has_file_axis_reference(question):
        allowed_sources = frozenset(("document",))
        axis = "file"
    elif not any(result.source == "document" for result in results):
        allowed_sources = frozenset(SOURCE_NAMES)
        axis = "non_file"
    else:
        document_relevant = any(
            result.source == "document"
            and isinstance(result.payload, Mapping)
            and result.payload.get("answer_eligible") is True
            for result in results
        )
        allowed_sources = (
            frozenset((*SOURCE_NAMES, "document"))
            if document_relevant
            else frozenset(SOURCE_NAMES)
        )
        axis = "uploaded_document" if document_relevant else "non_file"
    selected = tuple(results)
    excluded_sources: list[str] = []
    guidance_only_filter_applied = bool(
        axis == "file" and _has_unsupported_file_guidance(results)
    )
    if guidance_only_filter_applied:
        excluded_sources = list(
            dict.fromkeys(result.source for result in selected if result.source != "document")
        )
        selected = tuple(result for result in selected if result.source == "document")
    available_sources = {result.source for result in selected}
    requested_record_type = _record_type(_layout_axis_question(question, plan))
    requested_primary_source = (
        "document"
        if axis == "uploaded_document"
        else _PRIMARY_SOURCE_BY_RECORD_TYPE.get(requested_record_type or "")
    )
    primary_source = (
        requested_primary_source
        if requested_primary_source in allowed_sources
        else next(
            (source for source in plan.answer_sources if source in allowed_sources),
            None,
        )
    )
    if primary_source in available_sources:
        selected = (
            *(result for result in selected if result.source == primary_source),
            *(result for result in selected if result.source != primary_source),
        )
    followup_axis_filter_applied = False
    return selected, {
        "applied": bool(excluded_sources),
        "axis": axis,
        "excluded_sources": excluded_sources,
        "primary_source": primary_source,
        "primary_source_basis": (
            "requested_axis"
            if requested_primary_source is not None
            and primary_source == requested_primary_source
            else "planner_order"
        ),
        "primary_source_available": primary_source in available_sources,
        "priority_applied": bool(selected and selected[0].source == primary_source),
        "followup_axis_filter_applied": followup_axis_filter_applied,
        "all_received_sources_preserved": True,
        "guidance_only_filter_applied": guidance_only_filter_applied,
        "body_relevance_excluded_count": len(excluded_sources),
    }


def _has_unsupported_file_guidance(results: Sequence[SourceResult]) -> bool:
    """Identify deterministic file guidance without discarding retrieved lanes."""

    for result in results:
        if result.source != "document" or not isinstance(result.payload, Mapping):
            continue
        sql_trace = result.payload.get("sql_trace")
        if (
            isinstance(sql_trace, Sequence)
            and not isinstance(sql_trace, (str, bytes))
            and any(
                isinstance(item, Mapping)
                and str(item.get("status") or "") == "unsupported_dimension"
                for item in sql_trace
            )
        ):
            return True
        details = result.payload.get("file_tool_details")
        if isinstance(details, Mapping):
            document_sql = details.get("document_sql")
            if isinstance(document_sql, Mapping) and str(
                document_sql.get("status") or ""
            ) == "unsupported_dimension":
                return True
    return False


def _core_context_override(
    results: Sequence[SourceResult],
    card_core: str,
) -> str:
    """Keep execution-owned absence and cache context on the core surface."""
    for result in results:
        if not isinstance(result.payload, Mapping):
            continue
        raw = result.payload.get("absence_confirmation")
        if not isinstance(raw, Mapping):
            continue
        source = str(raw.get("source") or result.source)
        document = str(raw.get("doc_type") or "")
        status = str(raw.get("status") or "")
        subject = str(raw.get("subject") or "해당 대상")
        if source == "hira" and document == "reimbursement":
            if status == "confirmed_non_reimbursed":
                core = f"{subject}은 현재 급여기준이 없습니다(비급여). 고시 무결과 확인입니다."
            else:
                core = (
                    "현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다. "
                    "이 결과만으로 비급여 여부를 확정할 수는 없습니다. "
                    "고시 무결과 확인입니다."
                )
            web_context = _absence_web_context_sentence(results)
            if web_context:
                core = f"{core} {web_context}"
            return _append_reuse_marker(core, results)
        if source == "nedrug" and document == "approval":
            core = (
                f"현재 조회한 식품의약품안전처 자료에서는 {subject}의 허가 문서를 "
                "찾지 못했습니다. 이 결과만으로 허가 부재를 확정할 수는 없습니다. "
                "허가 문서 무결과 확인입니다."
            )
            return _append_reuse_marker(core, results)
    return _append_reuse_marker(card_core, results)


def _absence_web_context_sentence(results: Sequence[SourceResult]) -> str:
    for result in results:
        if result.source != "web" or result.status != "ok" or not isinstance(
            result.payload, Mapping
        ):
            continue
        items = result.payload.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            title = " ".join(str(item.get("title") or "").split())
            if not title:
                continue
            published = str(
                item.get("published_at")
                or item.get("published_date")
                or item.get("date")
                or ""
            ).strip()
            date_prefix = f"{published} 게시된 " if published else ""
            return f'공개 자료에서는 {date_prefix}"{title}" 경과가 확인됐습니다.'
    return ""


def _append_reuse_marker(core: str, results: Sequence[SourceResult]) -> str:
    return core


def _append_reuse_surface(
    answer: str,
    results: Sequence[SourceResult],
    *,
    reused: bool = False,
) -> str:
    return answer


def _prefer_generated_document_core(fact_digest: FactDigest) -> bool:
    if fact_digest.answer_type != "document_summary":
        return False
    return not any(
        card.card_type == "document_summary"
        and bool(
            card.file_facts.get("prefer_deterministic_core")
            or card.file_facts.get("targeted_facts")
        )
        for card in fact_digest.cards
    )


def _append_file_analytics_tables(
    answer_text: str,
    tables: Sequence[str],
) -> str:
    """Attach file tables after prose synthesis so SSE can emit table events."""

    parts = (
        answer_text.strip(),
        *(table.strip() for table in tables if table.strip()),
    )
    return "\n\n".join(part for part in parts if part)


def _without_rendered_tables(answer_text: str, tables: Sequence[str]) -> str:
    """Keep deterministic table rows out of LLM prose while preserving facts."""

    cleaned = answer_text
    for table in tables:
        cleaned = cleaned.replace(table, "")
    return "\n".join(line for line in cleaned.splitlines() if line.strip()).strip()


def _without_hira_placeholder_notice(answer_text: str) -> str:
    """Keep table-cell placeholder metadata out of HIRA answer prose."""

    return re.sub(
        r"\s*원천 미제공\s+\d+행은 표에서 제외했습니다\.?",
        "",
        answer_text,
    ).strip()


def _append_clinical_distribution_reference(
    answer_text: str,
    rendered: DeterministicRender,
    *,
    answer_type: str,
) -> tuple[str, dict[str, object]]:
    nodes = tuple(
        node
        for node in rendered.nodes
        if node.block_id.startswith("clinical:statistics:") and node.text.strip()
    )
    if not nodes:
        return answer_text, {"applied": False, "node_count": 0, "reason": "no_nodes"}
    if answer_type not in {"clinical", "disease", "mixed"}:
        return answer_text, {
            "applied": False,
            "node_count": len(nodes),
            "reason": "answer_type_not_clinical",
        }
    if "### 상태 분포" in answer_text:
        return answer_text, {
            "applied": False,
            "node_count": len(nodes),
            "reason": "already_present",
        }

    reference = "\n\n".join(node.text.strip() for node in nodes)
    reference = reference.replace(
        "## 임상시험 분포",
        "## 참고: 임상시험 분포",
        1,
    )
    return (
        f"{answer_text.rstrip()}\n\n{reference}",
        {"applied": True, "node_count": len(nodes), "reason": "appended"},
    )


def _finalize_post_semantic_insight(
    candidate_text: str,
    *,
    insight_candidate: str,
    insight_lane_trace: Mapping[str, Any],
    question: str,
    sources: Sequence[str],
    fact_digest: FactDigest | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    structured_verified = bool(
        insight_candidate
        and insight_lane_trace.get("status") == "completed"
        and insight_lane_trace.get("structured_output") is True
        and insight_lane_trace.get("fallback_required") is False
    )
    deterministic_l1 = bool(
        insight_candidate
        and insight_lane_trace.get("status") == "completed"
        and insight_lane_trace.get("ladder_level") == "L1"
        and insight_lane_trace.get("ladder_complete") is True
        and insight_lane_trace.get("fallback_required") is False
    )
    if structured_verified or deterministic_l1:
        verification = insight_lane_trace.get("claim_manifest", {}).get(
            "verification", {}
        )
        retained_count = len(verification.get("claims", ())) - int(
            verification.get("hard_block_count", 0) or 0
        )
        return candidate_text, {
            "contract_met": True,
            "omitted": False,
            "section_found": True,
            "candidate_sentence_count": retained_count,
            "retained_sentence_count": retained_count,
            "removed_sentence_count": 0,
            "soft_degraded": False,
            "safety_gate": (
                "structured_claim_verifier"
                if structured_verified
                else "dm_deterministic_l1"
            ),
        }, {
            "attempted": False,
            "applied": False,
            "reason": (
                "structured_claim_verifier_authoritative"
                if structured_verified
                else "dm_deterministic_l1_authoritative"
            ),
            "before_sentence_count": retained_count,
            "after_sentence_count": retained_count,
            "deterministic_sentence_count": 0,
            "deterministic_sentence_ratio": 0.0,
            "final_context_promotion": {
                "applied": False,
                "reason": "structured_claim_surface_present",
            },
        }

    final_text, contract_trace = ensure_s9_insight_surface(
        candidate_text,
        question=question,
        sources=sources,
        fact_digest=fact_digest,
    )
    expansion_trace: dict[str, Any] = {
        "attempted": False,
        "applied": False,
        "reason": "fact_digest_unavailable",
    }
    if fact_digest is not None:
        final_text, expansion_trace = expand_s17_insight_from_digest(
            final_text,
            fact_digest,
            repair_failed=bool(
                insight_lane_trace.get("attempted")
                and not insight_lane_trace.get("retry_sentence_count", 0)
            ),
        )
        final_text, promotion_trace = promote_context_to_s17_insight(
            final_text, fact_digest
        )
        expansion_trace["final_context_promotion"] = promotion_trace
    return final_text, contract_trace, expansion_trace


def _without_legacy_section_heading(text: str) -> str:
    return "\n".join(
        line
        for line in text.strip().splitlines()
        if line.strip() not in {"## 핵심 답", "## 종합 인사이트"}
    ).strip()


class V4Runtime:
    def __init__(
        self,
        *,
        planner: V4Planner,
        executor: ParallelSourceExecutor,
        synthesizer: V4Synthesizer,
        state_store: SessionStateStore | Any | None = None,
        entity_resolver: Any | None = None,
        disease_code_client: Any | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._synthesizer = synthesizer
        self._state_store = state_store
        self._entity_resolver = entity_resolver
        self._disease_code_client = disease_code_client
        self._synthesis_policy = SynthesisPolicy.from_env()
        self._insight_lane = InsightLane(
            synthesizer,
            timeout_s=self._synthesis_policy.insight_lane_timeout_s,
        )
        self._total_timeout_s = self._synthesis_policy.total_request_budget_s
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
        answer_ready_callback: AnswerReadyCallback | None = None,
        section_ready_callback: SectionReadyCallback | None = None,
        supplemental_results: Sequence[SourceResult] = (),
    ) -> V4Answer:
        started = time.monotonic()
        observed_on = current_kst_date()
        progress_events: list[dict[str, Any]] = []
        caller_progress_callback = progress_callback

        def record_progress(event: dict[str, Any]) -> None:
            stored_event = dict(event)
            stored_event.setdefault("recorded_at", utc_now().isoformat())
            progress_events.append(stored_event)
            if caller_progress_callback is not None:
                caller_progress_callback(event)

        progress_callback = record_progress
        deadline = started + self._total_timeout_s
        deadline_at = utc_now() + timedelta(seconds=self._total_timeout_s)
        selected_turns = tuple(turns)[-10:]
        prior_turn_context = build_prior_turn_context(question, selected_turns)
        if prior_turn_context.result is not None:
            supplemental_results = (
                *tuple(supplemental_results),
                prior_turn_context.result,
            )
        session_id = conversation_id or uuid4().hex
        # Everything from here to the T0 probe below has never been measured.
        # The timeline only buffers boundaries; it serializes at T0.
        pre_t0 = begin_pre_answer_timeline(session_id, question)
        session_state = self._state_store.load(session_id) if self._state_store is not None else None
        pre_t0.mark("pre_t0.session_load")
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
        pre_t0.mark(
            "pre_t0.planner",
            {
                "status": planner_outcome.trace.get("status"),
                "elapsed_ms": planner_outcome.trace.get("elapsed_ms"),
                "serving_id": planner_outcome.trace.get("serving_id"),
                "attempts": planner_outcome.trace.get("attempts"),
                "degradation_reason": planner_outcome.trace.get("degradation_reason"),
                "partial_plan_recovered": planner_outcome.trace.get("partial_plan_recovered"),
            },
        )
        planner_degradation_notice = _planner_degradation_notice(planner_outcome.trace)
        if planner_degradation_notice:
            LOGGER.warning(
                "v4 planner degraded status=%s reason=%s partial_plan_recovered=%s",
                planner_outcome.trace.get("status"),
                planner_outcome.trace.get("degradation_reason", "unknown"),
                planner_outcome.trace.get("partial_plan_recovered", False),
            )
        plan = _bind_always_on_mart_query(
            planner_outcome.plan,
            question,
            supplemental_results=supplemental_results,
        )
        plan = _bind_file_source_comparison_queries(plan, question)
        plan = _bind_mixed_axis_answer_sources(
            plan,
            question,
            supplemental_results=supplemental_results,
        )
        pre_t0.mark("pre_t0.plan_bind")
        plan = _bind_session_state_contract(plan, question, session_state)
        plan = _preserve_period_in_answer_queries(plan)
        fallback_routing = sanitize_planner_fallback(
            plan,
            question,
            planner_outcome.trace,
            resolver=self._entity_resolver,
            molecule_fallback=getattr(self._executor, "clinical_molecule_fallback", None),
        )
        plan = fallback_routing.plan
        explicit_substance = augment_explicit_substance_queries(
            plan,
            question,
            resolver=self._entity_resolver,
            molecule_fallback=getattr(self._executor, "clinical_molecule_fallback", None),
        )
        plan = explicit_substance.plan
        plan, entity_hygiene_trace = sanitize_planner_entities(
            question,
            plan,
            resolver=self._entity_resolver,
        )
        if prior_turn_context.triggered:
            plan = merge_prior_turn_entities(
                plan,
                prior_turn_context.merge_entities,
            )
        plan = fan_out_tier_zero_queries(plan)
        pre_t0.mark("pre_t0.plan_sanitize")
        parameter_expansion = expand_parameter_axes(
            plan,
            question,
            observed_on=observed_on,
            disease_code_client=self._disease_code_client,
        )
        pre_t0.mark(
            "pre_t0.parameter_expansion",
            {"disease_code_lookup": parameter_expansion.trace.get("disease_code_lookup")},
        )
        plan, grain_routing_trace = route_queries_by_grain(parameter_expansion.plan)
        plan = apply_source_call_cap(plan)
        plan, pruning_trace = prune_unsupported_source_queries(plan)
        parameter_expansion.trace["unsupported_source_pruning"] = pruning_trace
        parameter_expansion.trace["planner_fallback_routing"] = fallback_routing.trace
        parameter_expansion.trace["explicit_substance_routing"] = explicit_substance.trace
        parameter_expansion.trace["entity_hygiene"] = entity_hygiene_trace
        parameter_expansion.trace["grain_routing"] = grain_routing_trace
        parameter_expansion.trace["query_scope"] = (
            plan.query_scope.model_dump(mode="json") if plan.query_scope else None
        )
        clinical_query_anchor = _deterministic_clinical_query_anchor(
            question,
            session_state,
        )
        execution_plan, clinical_query_normalization = _execution_plan(
            self._executor,
            plan,
            clinical_query_anchor=clinical_query_anchor,
        )
        execution_plan, execution_grain_trace = route_queries_by_grain(execution_plan)
        parameter_expansion.trace["execution_grain_routing"] = execution_grain_trace
        execution_plan, axis_shaping_trace = shape_axis_queries(execution_plan, question)
        execution_plan, file_session_lane_trace = _bind_active_file_session_plan(
            execution_plan,
            question=question,
            supplemental_results=supplemental_results,
            resolver=self._entity_resolver,
        )
        plan, _display_file_session_lane_trace = _bind_active_file_session_plan(
            plan,
            question=question,
            supplemental_results=supplemental_results,
            resolver=self._entity_resolver,
        )
        include_document = any(
            result.source == "document" for result in supplemental_results
        )
        execution_plan = _reconcile_answer_contract(execution_plan, question)
        plan = _reconcile_answer_contract(plan, question)
        execution_plan, all_source_execution_trace = _force_all_source_plan(
            execution_plan,
            question=question,
            include_document=include_document,
            resolver=self._entity_resolver,
            molecule_fallback=getattr(
                self._executor, "clinical_molecule_fallback", None
            ),
        )
        plan, all_source_display_trace = _force_all_source_plan(
            plan,
            question=question,
            include_document=include_document,
            resolver=self._entity_resolver,
            molecule_fallback=getattr(
                self._executor, "clinical_molecule_fallback", None
            ),
        )
        if prior_turn_context.triggered:
            execution_plan = merge_prior_turn_entities(
                execution_plan,
                prior_turn_context.merge_entities,
            )
            plan = merge_prior_turn_entities(
                plan,
                prior_turn_context.merge_entities,
            )
        parameter_expansion.trace["axis_shaping"] = axis_shaping_trace
        parameter_expansion.trace["file_session_lane_binding"] = file_session_lane_trace
        parameter_expansion.trace["all_source_execution"] = all_source_execution_trace
        parameter_expansion.trace["all_source_display"] = all_source_display_trace
        parameter_expansion.trace["prior_turn"] = prior_turn_context.trace
        plan = plan.model_copy(update={"query_scope": execution_plan.query_scope})
        parameter_expansion.trace["query_scope"] = (
            plan.query_scope.model_dump(mode="json") if plan.query_scope else None
        )
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
        if prior_turn_context.triggered:
            _emit_progress(
                progress_callback,
                "조회 계획",
                "이전 대화에서 참조 근거 확인",
                raw_name="v4_source:prior_turn",
                raw_detail=prior_turn_context.reason,
            )
        source_queries = {
            source: _source_query_summary(queries)
            for source, queries in execution_plan.tool_queries.items()
        }
        source_expected = {
            source: len(queries)
            for source, queries in execution_plan.tool_queries.items()
        }
        # Derived from source_expected, which the answer path has already built,
        # so the probe never introduces a traversal of its own.
        pre_t0.mark(
            "pre_t0.execution_plan",
            {
                "planned_calls": sum(source_expected.values()),
                "lanes_planned": sorted(
                    source for source, count in source_expected.items() if count
                ),
            },
        )
        source_results: dict[str, list[SourceResult]] = {
            source: [] for source in SOURCE_NAMES
        }
        source_completed_at: dict[str, str] = {}
        source_completed_monotonic: dict[str, float] = {}
        streamed_prefix = ""
        partial_stream_safe = _partial_axis_stream_is_safe(
            question,
            plan.answer_sources,
        )
        partial_stream_trace: dict[str, Any] = {
            "eligible": len(question_axes(question)) > 1 and partial_stream_safe,
            "emitted": False,
            "deferred_reason": (
                None if partial_stream_safe else "awaiting_all_requested_answer_axes"
            ),
        }
        for source in SOURCE_NAMES:
            if source_expected[source] == 0:
                continue
            _emit_source_progress(
                progress_callback,
                source=source,
                query=source_queries[source],
                results=(),
                expected=source_expected[source],
            )

        def source_completed(result_or_source: SourceResult | str) -> None:
            nonlocal streamed_prefix
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
            source_completed_at[result.source] = utc_now().isoformat()
            source_completed_monotonic[result.source] = time.monotonic()
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
            partial_stream_trace["deferred_reason"] = "awaiting_all_lanes"

        prior_results = self._get_session_results(session_id)
        if not prior_results and session_state is not None:
            prior_results = _results_from_session_state(session_state)
        can_reuse_prior = (
            allow_legacy_result_reuse(prior_turn_context)
            and
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
                execution_plan,
                session_id=session_id,
                total_timeout_s=min(total_timeout_s(), _remaining(deadline)),
                answer_sources=plan.answer_sources,
                settle_sources=("mart",),
                soft_deadline_s=6.0,
                soft_deadline_exempt_sources=_soft_deadline_exempt_sources(
                    plan.resolved_question
                ),
                progress_callback=source_completed,
                source_filter=None,
            )
            first_execution.trace.setdefault("session_result_reused", False)
        pre_t0.mark(
            "pre_t0.fanout_first",
            lane_spans_from_execution_trace(first_execution.trace),
        )
        first_results = first_execution.results
        deterministic_link = (
            build_second_hop_expansion(
                plan,
                question,
                first_results,
                max_queries=3,
            )
            if (
                not first_execution.trace["session_result_reused"]
                and _needs_deterministic_expansion(plan, question)
            )
            else None
        )
        linked_plan = deterministic_link.plan if deterministic_link is not None else None
        if linked_plan is None and (
            plan.needs_second_hop
            and not first_execution.trace["session_result_reused"]
            and _remaining(deadline) > 1.0
        ):
            linked_plan = _call_with_state(
                self._planner.link,
                plan,
                first_results,
                selected_turns,
                budget_s=min(7.0, _remaining(deadline)),
                state=session_state,
            )
        if linked_plan is not None:
            _emit_progress(
                progress_callback,
                "연결 조회",
                "첫 조회에서 확인한 대상을 바탕으로 관련 자료를 한 번 더 조회합니다",
            )
        if linked_plan is not None:
            linked_composition = compose_all_source_queries(
                linked_plan,
                question,
                resolver=self._entity_resolver,
                molecule_fallback=getattr(
                    self._executor,
                    "clinical_molecule_fallback",
                    None,
                ),
            )
            linked_plan = linked_composition.plan
            linked_explicit_substance = augment_explicit_substance_queries(
                linked_plan,
                question,
                resolver=self._entity_resolver,
                molecule_fallback=getattr(
                    self._executor,
                    "clinical_molecule_fallback",
                    None,
                ),
            )
            linked_plan = linked_explicit_substance.plan
            linked_execution_plan, linked_clinical_normalization = _execution_plan(
                self._executor,
                linked_plan,
                clinical_query_anchor=_linked_clinical_query_anchor(
                    clinical_query_anchor,
                    linked_plan.resolved_question,
                ),
            )
            linked_execution_plan = _exclude_first_hop_queries(
                execution_plan,
                linked_execution_plan,
            )
            if not linked_execution_plan.answer_sources:
                linked_execution_plan = None
        else:
            linked_execution_plan = None
            linked_clinical_normalization = _clinical_normalization_trace(None, None)
            linked_composition = None
            linked_explicit_substance = None
        linked_execution = (
            _execute_with_trace(
                self._executor,
                linked_execution_plan,
                session_id=session_id,
                total_timeout_s=min(30.0, _remaining(deadline)),
                answer_sources=linked_execution_plan.answer_sources,
                source_filter=linked_execution_plan.answer_sources,
                soft_deadline_s=6.0,
                soft_deadline_exempt_sources=_soft_deadline_exempt_sources(
                    linked_plan.resolved_question
                ),
                progress_callback=source_completed,
            )
            if linked_execution_plan is not None and _remaining(deadline) > 0.1
            else SimpleNamespace(results=(), trace=None)
        )
        pre_t0.mark(
            "pre_t0.fanout_linked",
            lane_spans_from_execution_trace(linked_execution.trace),
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
        pre_t0.mark(
            "pre_t0.fanout_gap",
            lane_spans_from_execution_trace(gap_execution.trace),
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
        pre_t0.mark(
            "pre_t0.fanout_absence",
            lane_spans_from_execution_trace(absence_execution.trace),
        )
        current_results = (
            *first_results,
            *linked_results,
            *gap_execution.results,
            *absence_execution.results,
            *supplemental_results,
        )
        prior_turn_context = finalize_prior_turn_requery(
            prior_turn_context,
            tuple(
                result for result in current_results if result.source != "prior_turn"
            ),
        )
        if prior_turn_context.result is not None:
            current_results = (
                *(result for result in current_results if result.source != "prior_turn"),
                prior_turn_context.result,
            )
        for source in SOURCE_NAMES:
            if source_expected[source] == 0:
                continue
            missing_results = max(
                0,
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
        if allow_legacy_result_reuse(prior_turn_context) and prior_results and (
            _is_causal_followup(question)
            or (_is_prior_result_reference(question) and not can_reuse_prior)
        ):
            current_results = _merge_results(prior_results, current_results)
        results = tuple(_mark_citations_used(result) for result in current_results)
        self._remember_session_results(session_id, results)
        pre_t0.mark("pre_t0.merge_results", {"record_count": len(results)})
        answer_surface_followup = bool(
            prior_results
            and (
                can_reuse_prior
                or _is_prior_result_reference(question)
                or _is_causal_followup(question)
                or (
                    session_state is not None
                    and _should_inherit_session_contract(question, session_state)
                )
            )
        )
        answer_results, answer_surface_trace = _answer_surface_results(
            question,
            plan,
            results,
            followup=answer_surface_followup,
        )
        grounding_future = _submit_grounding_ledger(answer_results)
        pre_t0.mark(
            "pre_t0.answer_surface",
            {"answer_record_count": len(answer_results)},
        )
        lossless_mode = configured_lossless_mode()
        requested_fields_mode = configured_requested_fields_mode()
        request_satisfaction_mode = configured_request_satisfaction_mode()
        lossless_error_type: str | None = None
        try:
            evidence_sets, deterministic_render = build_lossless_render(
                plan,
                answer_results,
                observed_on=observed_on,
                source_render_limit=self._synthesis_policy.source_render_limit,
            )
            lane_execution = build_lane_execution_records(plan, results, evidence_sets)
        except LosslessInvariantError:
            LOGGER.exception("v4 lossless spine invariant failed")
            raise
        except Exception as exc:
            LOGGER.exception("v4 lossless spine build failed")
            evidence_sets = ()
            deterministic_render = DeterministicRender(profile="market_analysis")
            lane_execution = build_lane_execution_records(plan, results)
            lossless_error_type = type(exc).__name__
        pre_t0.mark(
            "pre_t0.lossless_render",
            {
                "evidence_sets": len(evidence_sets),
                "lossless_error_type": lossless_error_type,
                "lane_execution_rows": len(lane_execution),
            },
        )
        visible_facts = deterministic_fact_text(
            deterministic_render,
            requested_fields_mode,
        )
        surface_question = _surface_contract_question(question, plan)
        prefer_document_surface = answer_surface_trace.get("axis") in {
            "file",
            "uploaded_document",
        }
        fact_digest = build_fact_digest(
            surface_question,
            evidence_sets,
            deterministic_render,
            prefer_document=prefer_document_surface,
            observed_on=observed_on,
            answer_contract=plan.answer_contract,
        )
        parameter_expansion.trace["period_scope"] = period_scope_trace(fact_digest)
        answer_contract_coverage = fact_digest_contract_coverage(
            fact_digest,
            source_states={
                source: record.state.name for source, record in lane_execution.items()
            },
        )
        document_fact_ids = tuple(
            dict.fromkeys(
                evidence_id
                for card in fact_digest.cards
                if card.source == "document"
                for evidence_id in card.evidence_ids
            )
        )
        core_generation_started_at = utc_now().isoformat()
        core_generation_started_monotonic = time.monotonic()
        rendered_core = render_core_answer(fact_digest)
        file_analytics_tables = render_file_analytics_tables(fact_digest)
        hira_statistics_tables = render_hira_statistics_tables(fact_digest)
        card_core = _core_context_override(
            answer_results,
            rendered_core,
        )
        source_failure_core = _source_failure_core(results)
        deterministic_facts = (
            _without_rendered_tables(visible_facts, hira_statistics_tables)
            if lossless_mode == "inject"
            and deterministic_render.profile != "market_analysis"
            and visible_facts
            else None
        )
        request_context_notices = tuple(
            dict.fromkeys(
                notice
                for notice in (
                    _session_inheritance_notice(question, session_state),
                    _query_scope_notice(plan),
                    _retrieval_shortfall_notice(results),
                )
                if notice
            )
        )
        if request_context_notices:
            existing_notice = str(deterministic_render.request_notice or "").strip()
            deterministic_render = deterministic_render.model_copy(
                update={
                    "request_notice": "\n".join(
                        notice
                        for notice in (existing_notice, *request_context_notices)
                        if notice
                    )
                }
            )
        # A document summary's generated core owns the answer. Streaming the
        # deterministic body first would make a provisional failure immutable.
        pre_t0.mark(
            "pre_t0.answer_ready_deferred",
            {"reason": "awaiting_final_synthesis"},
        )
        _emit_progress(
            progress_callback,
            "답변 작성 중",
            "확인된 근거를 종합해 답변을 작성합니다",
        )
        synthesize_with_trace = getattr(self._synthesizer, "synthesize_with_trace", None)
        synthesis_budget_s = self._synthesis_policy.allocate_synthesis_budget(
            remaining_s=_remaining(deadline)
        )
        if synthesis_budget_s is None:
            synthesis = SynthesisOutcome(
                text="해설은 생략하고 조회 결과만 표시합니다.",
                trace={
                    "status": "budget_skipped",
                    "fallback_reason": "insufficient_remaining_budget",
                    "partial_generated": False,
                    "budget": {
                        "remaining_s": _remaining(deadline),
                        "minimum_s": self._synthesis_policy.min_synthesis_budget_s,
                        "attempted": False,
                    },
                },
            )
        elif callable(synthesize_with_trace):
            try:
                synthesis = _call_with_state(
                    synthesize_with_trace,
                    plan,
                    answer_results,
                    selected_turns,
                    budget_s=synthesis_budget_s,
                    state=session_state,
                    optional_kwargs={
                        "deterministic_facts": deterministic_facts,
                        "fact_digest": fact_digest,
                        "core_only": True,
                        "defer_market_facts": bool(
                            lossless_mode == "inject"
                            and deterministic_render.profile == "market_analysis"
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - invariant 3: commentary may fail, facts may not
                synthesis = _synthesis_failure_outcome(exc)
        else:
            try:
                synthesis = SynthesisOutcome(
                    text=_call_with_state(
                        self._synthesizer.synthesize,
                        plan,
                        answer_results,
                        selected_turns,
                        budget_s=synthesis_budget_s,
                        state=session_state,
                        optional_kwargs={
                            "deterministic_facts": deterministic_facts,
                            "fact_digest": fact_digest,
                            "core_only": True,
                            "defer_market_facts": bool(
                                lossless_mode == "inject"
                                and deterministic_render.profile == "market_analysis"
                            ),
                        },
                    ),
                    trace={},
                )
            except Exception as exc:  # noqa: BLE001 - invariant 3: commentary may fail, facts may not
                synthesis = _synthesis_failure_outcome(exc)
        pre_t0.mark(
            "pre_t0.synthesis",
            {
                "status": synthesis.trace.get("status"),
                "elapsed_ms": synthesis.trace.get("elapsed_ms"),
                "serving_id": synthesis.trace.get("serving_id"),
                "prompt_chars": synthesis.trace.get("prompt_chars"),
                "raw_payload_chars": synthesis.trace.get("raw_payload_chars"),
                "budget_s": synthesis_budget_s,
                "attempts": synthesis.trace.get("attempts"),
            },
        )
        gated = apply_v4_gates(plan.resolved_question, synthesis.text, answer_results)
        pre_t0.mark("pre_t0.gates")
        composition = compose_lossless_answer(
            deterministic_render,
            gated.text,
            synthesis_trace=synthesis.trace,
            mode=lossless_mode,
            requested_fields_mode=requested_fields_mode,
            request_satisfaction_mode=request_satisfaction_mode,
            question=_layout_axis_question(
                question,
                plan,
                prefer_document=prefer_document_surface,
            ),
            streamed_prefix=streamed_prefix,
        )
        pre_t0.mark("pre_t0.compose")
        final_text = _without_rendered_tables(
            composition.text,
            hira_statistics_tables,
        )
        completed_at = utc_now()
        retrieval_events = tuple(
            retrieval_event_from_result(
                result,
                entity_id=_result_entity_id(plan, result),
                completed_at=completed_at,
                deadline_at=deadline_at,
            )
            for result in results
        )
        pre_t0.mark("pre_t0.retrieval_events", {"events": len(retrieval_events)})
        entity_completion = entity_completion_rows(plan, answer_results)
        final_text, entity_surface_trace = _inject_entity_completion_surface(
            final_text,
            entity_completion,
            current_question=question,
            results=answer_results,
        )
        final_text, surface_binding_trace = sanitize_bound_surface(
            "\n".join(
                (
                    question,
                    *plan.requested_answer_shape.entities,
                )
            ),
            final_text,
            evidence_sets,
            retrieval_events,
        )
        completion_rows = tuple(entity_completion.rows)
        semantic_context = SemanticEvidenceContext(
            has_temporal_support=evidence_has_temporal_support(evidence_sets),
            supported_text=evidence_support_text(evidence_sets),
            temporal_support_texts=evidence_temporal_support_texts(evidence_sets),
            observed_count=sum(
                row.get("status") == "COMPLETE" for row in completion_rows
            ),
            requested_count=len(completion_rows),
            has_hira_patient_count=evidence_has_hira_patient_count(evidence_sets),
            hira_code_count=evidence_hira_code_count(evidence_sets),
            protected_line_sha256=tuple(
                sha256(line.strip().encode("utf-8")).hexdigest()
                for node in deterministic_render.nodes
                if node.block_id == "narrative:field-restatement"
                for line in node.text.splitlines()
                if line.strip()
            ),
        )
        semantic_surface = realize_semantic_surface(
            final_text,
            semantic_context,
        )
        semantic_text = semantic_surface.text
        post_semantic_retry_trace: dict[str, Any] = {
            "attempted": False,
            "candidate_available": False,
            "reason": "split_insight_lane",
            "retry_sentence_count": 0,
        }
        available_surface_axes = visible_surface_axes(deterministic_render)
        core_answer_integrity = ensure_core_answer_surface(
            semantic_text,
            surface_question,
            fallback_fact_body=tuple(
                line
                for node in deterministic_render.nodes
                for line in node.text.splitlines()
            ),
            available_axes=available_surface_axes,
            card_core=card_core or None,
            failure_core=source_failure_core,
            prefer_generated_core=_prefer_generated_document_core(fact_digest),
        )
        core_answer_text = _append_reuse_surface(
            core_answer_integrity.text,
            answer_results,
            reused=bool(can_reuse_prior),
        )
        core_answer_text, s17_body_cleanup_trace = strip_s17_body_metadata(
            core_answer_text
        )
        core_answer_text, core_answer_placement_trace = align_core_answer_to_question(
            core_answer_text,
            surface_question,
        )
        (
            core_answer_text,
            clinical_distribution_reference_trace,
        ) = _append_clinical_distribution_reference(
            core_answer_text,
            deterministic_render,
            answer_type=fact_digest.answer_type,
        )
        core_surface_ready_at = utc_now().isoformat()
        effective_section_ready_callback = section_ready_callback
        document_execution = lane_execution.get("document_rag")
        if (
            section_ready_callback is not None
            and document_execution is not None
            and document_execution.call_count > 0
        ):
            document_chunk_count = document_execution.returned_count

            def emit_scoped_section(event: dict[str, Any]) -> None:
                scoped_event = dict(event)
                if isinstance(scoped_event.get("text"), str):
                    scoped_event["text"] = scope_document_absence_claims(
                        scoped_event["text"],
                        received_count=document_chunk_count,
                    )
                section_ready_callback(scoped_event)

            effective_section_ready_callback = emit_scoped_section
        if effective_section_ready_callback is not None and prior_turn_context.triggered:
            downstream_section_callback = effective_section_ready_callback

            def emit_prior_turn_section(event: dict[str, Any]) -> None:
                scoped_event = dict(event)
                if (
                    scoped_event.get("section_id") == "facts"
                    and scoped_event.get("status") == "complete"
                    and isinstance(scoped_event.get("text"), str)
                ):
                    scoped_event["text"] = append_prior_turn_annotation(
                        scoped_event["text"],
                        prior_turn_context,
                    )
                    evidence = list(scoped_event.get("evidence") or ())
                    if not any(
                        isinstance(item, Mapping)
                        and item.get("evidence_id") == PRIOR_TURN_EVIDENCE_ID
                        for item in evidence
                    ):
                        evidence.append(prior_turn_evidence_reference(prior_turn_context))
                    scoped_event["evidence"] = evidence
                downstream_section_callback(scoped_event)

            effective_section_ready_callback = emit_prior_turn_section
        insight_lane_outcome = None
        if fact_digest is not None:
            insight_lane_outcome = self._insight_lane.generate(
                plan,
                core_answer_text,
                fact_digest=fact_digest,
                remaining_s=_remaining(deadline),
                section_ready_callback=effective_section_ready_callback,
                source_context={
                    "guidance_only_filter_applied": bool(
                        answer_surface_trace.get("guidance_only_filter_applied")
                    ),
                    "lanes": {
                        source: record.model_dump(mode="json")
                        for source, record in lane_execution.items()
                    },
                },
            )
            post_semantic_retry_trace = dict(insight_lane_outcome.trace)
        insight_candidate = (
            insight_lane_outcome.text
            if insight_lane_outcome is not None and insight_lane_outcome.text
            else ""
        )
        section_facts = (
            insight_lane_outcome.facts_text
            if insight_lane_outcome is not None
            else _without_legacy_section_heading(core_answer_text)
        )
        section_insight = (
            insight_lane_outcome.insight_text
            if insight_lane_outcome is not None
            else "확인된 사실을 바탕으로 추가 해석을 구성하지 못했습니다."
        )
        section_facts = append_prior_turn_annotation(
            section_facts,
            prior_turn_context,
        )
        claim_manifest = post_semantic_retry_trace.get("claim_manifest", {})
        section_paragraphs = (
            claim_manifest.get("section_paragraphs", {})
            if isinstance(claim_manifest, Mapping)
            else {}
        )
        if prior_turn_context.triggered:
            facts_paragraphs = list(section_paragraphs.get("facts") or ())
            if not any(
                PRIOR_TURN_EVIDENCE_ID in tuple(paragraph.get("evidence_ids") or ())
                for paragraph in facts_paragraphs
                if isinstance(paragraph, Mapping)
            ):
                facts_paragraphs.append(
                    {
                        "text": section_facts.rsplit("\n\n", 1)[-1],
                        "evidence_ids": (PRIOR_TURN_EVIDENCE_ID,),
                        "source_groups": (),
                        "unsourced": False,
                    }
                )
            section_paragraphs = {**section_paragraphs, "facts": facts_paragraphs}
        if document_execution is not None and document_execution.call_count > 0:
            document_chunk_count = document_execution.returned_count
            section_facts = scope_document_absence_claims(
                section_facts,
                received_count=document_chunk_count,
            )
            section_insight = scope_document_absence_claims(
                section_insight,
                received_count=document_chunk_count,
            )
            section_paragraphs = {
                section_id: [
                    {
                        **paragraph,
                        "text": scope_document_absence_claims(
                            str(paragraph.get("text") or ""),
                            received_count=document_chunk_count,
                        ),
                    }
                    if isinstance(paragraph, Mapping)
                    else paragraph
                    for paragraph in paragraphs
                ]
                for section_id, paragraphs in section_paragraphs.items()
            }
        referenced_evidence_ids = _section_paragraph_evidence_ids(section_paragraphs)
        evidence_display_catalog, evidence_display_trace = (
            build_evidence_display_catalog(
                evidence_sets,
                evidence_ids=referenced_evidence_ids,
            )
        )
        candidate_text = core_answer_text
        if insight_candidate:
            candidate_text = f"{candidate_text.rstrip()}\n\n{insight_candidate}"
        (
            final_text,
            post_semantic_insight_trace,
            post_semantic_expansion_trace,
        ) = _finalize_post_semantic_insight(
            candidate_text,
            insight_candidate=insight_candidate,
            insight_lane_trace=post_semantic_retry_trace,
            question=surface_question,
            sources=visible_s9_sources(deterministic_render),
            fact_digest=fact_digest,
        )
        if hira_statistics_tables:
            final_text = _without_hira_placeholder_notice(final_text)
        synthesis_insight_retry = synthesis.trace.get("insight_richness_retry", {})
        if not isinstance(synthesis_insight_retry, Mapping):
            synthesis_insight_retry = {}
        insight_sentence_provenance = {
            "generated_sentence_count": int(
                synthesis_insight_retry.get("generated_sentence_count", 0) or 0
            ),
            "retry_sentence_count": max(
                int(synthesis_insight_retry.get("retry_sentence_count", 0) or 0),
                int(post_semantic_retry_trace.get("retry_sentence_count", 0) or 0),
            ),
            "deterministic_sentence_count": int(
                post_semantic_expansion_trace.get(
                    "deterministic_sentence_count", 0
                )
                or 0
            ),
            "deterministic_sentence_ratio": float(
                post_semantic_expansion_trace.get(
                    "deterministic_sentence_ratio", 0.0
                )
                or 0.0
            ),
            "final_sentence_count": int(
                post_semantic_expansion_trace.get("after_sentence_count", 0) or 0
            ),
        }
        if core_answer_integrity.status != "present":
            LOGGER.warning(
                "v4 core answer integrity fallback status=%s reason=%s",
                core_answer_integrity.status,
                core_answer_integrity.reason,
            )
        pre_t0.mark(
            "pre_t0.surface_binding",
            {"s17_body_cleanup": s17_body_cleanup_trace},
        )
        final_narrative_metrics = measure_final_narrative_surface(
            final_text,
            evidence_sets,
            deterministic_render.record_field_usage,
        )
        pre_t0.mark("pre_t0.narrative_metrics")
        claim_ir_input_sha256 = sha256(final_text.encode("utf-8")).hexdigest()
        claim_ir_enabled = _claim_ir_shadow_enabled()
        if claim_ir_enabled:
            try:
                claim_classification = classify_answer_claims(final_text, evidence_sets)
                classifier_output_sha256 = sha256(
                    claim_classification.answer.encode("utf-8")
                ).hexdigest()
                classifier_attempted_mutation = (
                    claim_classification.answer_mutation
                    or classifier_output_sha256 != claim_ir_input_sha256
                )
                claim_ir_trace = {
                    "enabled": True,
                    "status": (
                        "contract_violation"
                        if classifier_attempted_mutation
                        else "classified"
                    ),
                    # This is the observed answer-path result. The classifier's
                    # attempted mutation is retained separately for diagnostics.
                    "answer_mutation": False,
                    "classifier_attempted_mutation": classifier_attempted_mutation,
                    "input_answer_sha256": claim_ir_input_sha256,
                    "output_answer_sha256": claim_ir_input_sha256,
                    "classifier_output_answer_sha256": classifier_output_sha256,
                    "claim_ir": [
                        claim.model_dump(mode="json")
                        for claim in claim_classification.claim_ir
                    ],
                    "recomputation_evidence": list(
                        claim_classification.recomputation_evidence
                    ),
                    "density_metrics": {
                        **claim_classification.density_metrics,
                        "gate_deletion_rate": _gate_deletion_rate(
                            synthesis.text,
                            gated.text,
                        ),
                    },
                }
            except Exception as exc:
                LOGGER.exception("v4 claim IR shadow classification failed")
                claim_ir_trace = {
                    "enabled": True,
                    "status": "error",
                    "answer_mutation": False,
                    "input_answer_sha256": claim_ir_input_sha256,
                    "output_answer_sha256": claim_ir_input_sha256,
                    "error_type": type(exc).__name__,
                    "claim_ir": [],
                    "recomputation_evidence": [],
                    "density_metrics": {},
                }
        else:
            claim_ir_trace = {
                "enabled": False,
                "status": "disabled",
                "answer_mutation": False,
                "input_answer_sha256": claim_ir_input_sha256,
                "output_answer_sha256": claim_ir_input_sha256,
                "claim_ir": [],
                "recomputation_evidence": [],
                "density_metrics": {},
            }
        pre_t0.mark("pre_t0.claim_ir_shadow", {"enabled": claim_ir_enabled})
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
        record_count = (
            int(final_narrative_metrics["narrated_record_count"])
            + int(final_narrative_metrics["unnarrated_record_count"])
        )
        pre_t0.mark("pre_t0.grounding_shadow")
        # T0. Everything above is now reported in one batch; the reconciliation
        # residue is emitted as unattributed_ms rather than hidden.
        pre_t0.flush(
            extra={
                "runtime_elapsed_ms": round(elapsed_ms, 3),
                "record_count": record_count,
                "total_timeout_s": self._total_timeout_s,
            }
        )
        latency_probe = begin_latency_probe(
            session_id,
            input_bytes=None,
            question=question,
            record_count=record_count,
        )
        raw_payload_bytes = latency_probe.checkpoint(
            "runtime.raw_payload",
            output_value=[result.payload for result in results],
            object_count=len(results),
            fields={
                "raw_payload_chars": synthesis.trace.get("raw_payload_chars"),
                "prompt_chars": synthesis.trace.get("prompt_chars"),
            },
        )
        synth_usage = _normalized_synth_usage(synthesis.trace)
        planner_usage = _normalized_planner_usage(
            planner_outcome.trace.get("usage"), planner_outcome.trace.get("thinking")
        )
        stage_timing = {
            "planner_elapsed_ms": planner_outcome.trace.get("elapsed_ms"),
            "wave_elapsed_ms": first_execution.trace.get("elapsed_ms"),
            "link_wave_elapsed_ms": (
                linked_execution.trace.get("elapsed_ms")
                if isinstance(linked_execution.trace, dict)
                else None
            ),
            "synth_elapsed_ms": synthesis.trace.get("elapsed_ms"),
            "insight_elapsed_ms": post_semantic_retry_trace.get("elapsed_ms"),
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
            "fallback": planner_outcome.trace.get("status") == "fallback",
            "planner_degradation": {
                "status": planner_outcome.trace.get("status", "unknown"),
                "reason": planner_outcome.trace.get("degradation_reason"),
                "partial_plan_recovered": planner_outcome.trace.get(
                    "partial_plan_recovered", False
                ),
                "notice_shown": False,
            },
            "planner": plan.model_dump(mode="json"),
            "prior_turn": prior_turn_context.trace,
            "execution_plan": execution_plan.model_dump(mode="json"),
            "answer_surface_scope": answer_surface_trace,
            "partial_stream": partial_stream_trace,
            "generation_order": {
                "lane_completed_at": source_completed_at,
                "core_generation_started_at": core_generation_started_at,
                "core_started_after_last_lane": bool(
                    source_completed_monotonic
                    and core_generation_started_monotonic
                    > max(source_completed_monotonic.values())
                ),
            },
            "lane_execution": {
                source: record.model_dump(mode="json")
                for source, record in lane_execution.items()
            },
            "clinical_query_normalization": clinical_query_normalization,
            "planner_usage": planner_usage,
            "second_hop": linked_plan.model_dump(mode="json") if linked_plan else None,
            "expansion": {
                "parameter_axes": parameter_expansion.trace,
                "second_hop": (
                    deterministic_link.trace if deterministic_link is not None else None
                ),
            },
            "linked_clinical_query_normalization": linked_clinical_normalization,
            "linked_query_composition": (
                linked_composition.trace if linked_composition is not None else None
            ),
            "linked_explicit_substance_routing": (
                linked_explicit_substance.trace
                if linked_explicit_substance is not None
                else None
            ),
            "tool_results": [
                {
                    "sequence": index,
                    "source": result.source,
                    "query": result.query,
                    "status": result.status,
                    "elapsed_ms": result.elapsed_ms,
                    "cache_hit": result.cache_hit,
                    "notice": result.notice,
                    "failure_reason": result.failure_reason,
                    "failure_detail": result.failure_detail,
                    "citations": [
                        citation.model_dump(mode="json")
                        for citation in result.citations
                    ],
                    "payload": result.payload,
                }
                for index, result in enumerate(results, start=1)
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
            "insight_lane": post_semantic_retry_trace,
            "late_binding": {
                "core_surface_ready_at": core_surface_ready_at,
                "wire_format": "jw.answer-sections.v1",
            },
            "answer_sections": {
                "schema": "jw.answer-sections.v1",
                "sections": [
                    {
                        "id": "insight",
                        "order": 0,
                        "kind": "insight",
                        "status": "pending",
                    },
                    {
                        "id": "facts",
                        "order": 1,
                        "kind": "facts",
                        "title": "조사 결과",
                        "status": "pending",
                    },
                ],
                "content": {
                    "insight": section_insight,
                    "facts": section_facts,
                },
                "evidence_catalog": evidence_display_catalog,
                "evidence_catalog_trace": evidence_display_trace,
                "paragraphs": section_paragraphs,
            },
            "gates": gated.trace,
            "selection_rule": composition.trace.get("selection_rule"),
            "selection_is_ranked": composition.trace.get("selection_is_ranked"),
            "lossless_spine": {
                **composition.trace,
                **final_narrative_metrics,
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
            "surface_binding": surface_binding_trace,
            "semantic_realization": semantic_surface.model_dump(mode="json"),
            "post_semantic_insight": post_semantic_insight_trace,
            "post_semantic_insight_retry": post_semantic_retry_trace,
            "post_semantic_insight_expansion": post_semantic_expansion_trace,
            "insight_sentence_provenance": insight_sentence_provenance,
            "core_answer_integrity": core_answer_integrity.model_dump(
                mode="json",
                exclude={"text"},
            ),
            "core_answer_placement": core_answer_placement_trace,
            "clinical_distribution_reference": clinical_distribution_reference_trace,
            "fact_digest": {
                "answer_type": fact_digest.answer_type,
                "card_count": len(fact_digest.cards),
                "derived_metrics": [
                    metric.model_dump(mode="json")
                    for metric in fact_digest.derived_metrics
                ],
                "derived_metrics_manifest": (
                    fact_digest.derived_metrics_manifest.model_dump(mode="json")
                ),
                "source_received_counts": fact_digest.source_received_counts,
                "source_visible_counts": fact_digest.source_visible_counts,
                "body_relevance": body_relevance_trace(fact_digest),
                "visible_record_count": len(fact_digest.visible_record_ids),
                "prompt_payload_sha256": sha256(
                    repr(fact_digest.prompt_payload()).encode("utf-8")
                ).hexdigest(),
                "card_core_sha256": (
                    sha256(card_core.encode("utf-8")).hexdigest()
                    if card_core
                    else None
                ),
                "answer_contract_coverage": (
                    asdict(answer_contract_coverage)
                    if answer_contract_coverage is not None
                    else None
                ),
            },
            "available_surface_axes": list(available_surface_axes),
            "retrieval_events": [
                event.model_dump(mode="json") for event in retrieval_events
            ],
            "source_tier_funnel": tier_funnel(
                plan,
                results,
                evidence_sets,
                tuple(
                    record_id
                    for node in deterministic_render.nodes
                    for record_id in node.record_ids
                ),
                tuple(
                    argument["record_id"]
                    for claim in claim_ir_trace["claim_ir"]
                    for argument in claim["arguments"]
                ),
            ),
            "entity_completion": {
                "rows": list(entity_completion.rows),
                "entity_types": list(entity_completion.entity_types),
                "scope_notice": entity_completion.scope_notice,
                "excluded_render_axes": list(
                    render_axis_tokens(plan.requested_answer_shape.entities)
                ),
                "surface": entity_surface_trace,
                "snapshot_sha256": sha256(
                    repr(entity_completion.rows).encode("utf-8")
                ).hexdigest(),
            },
            "claim_ir_shadow": claim_ir_trace,
            "claim_ir_realization": {
                "claim_ir": list(deterministic_render.structured_claims),
                "recomputation_evidence": list(
                    deterministic_render.structured_recomputations
                ),
                "truncated_t2_count": (
                    deterministic_render.structured_claims_truncated
                ),
                "unnarrated_record_count": (
                    final_narrative_metrics["unnarrated_record_count"]
                ),
                "narrated_record_count": final_narrative_metrics["narrated_record_count"],
                "narrated_record_ids": final_narrative_metrics["narrated_record_ids"],
                "unnarrated_records": final_narrative_metrics["unnarrated_records"],
                "record_field_usage": final_narrative_metrics["record_field_usage"],
                "average_narrated_field_count": (
                    final_narrative_metrics["average_narrated_field_count"]
                ),
                "loaded_field_narrative_use_rate": (
                    final_narrative_metrics["loaded_field_narrative_use_rate"]
                ),
                "identifier_only_sentence_count": (
                    final_narrative_metrics["identifier_only_sentence_count"]
                ),
                "answer_mutation": composition.answer_mutated,
            },
            "typed_grounding_shadow": grounding_shadow,
            "progress_events": progress_events,
            "progress_restoration": {
                "storage": "conversation_trace_json",
                "event_count": len(progress_events),
                "ddl_required": False,
            },
        }
        trace_bytes = latency_probe.checkpoint(
            "runtime.trace_source_funnel",
            input_bytes=raw_payload_bytes,
            output_value=trace,
            object_count=len(trace),
        )
        rendered_record_ids = tuple(
            record_id
            for node in deterministic_render.nodes
            for record_id in node.record_ids
        )
        rendered_record_ids = _chart_record_ids(
            evidence_sets,
            rendered_record_ids,
            question=plan.resolved_question,
            per_source_limit=self._synthesis_policy.source_render_limit,
        )
        charts = build_grounded_charts(
            evidence_sets,
            rendered_record_ids,
            question=plan.resolved_question,
        )
        requested_chart = requested_chart_metric(plan.resolved_question)
        chart_reason = (
            "grounded_series"
            if charts
            else requested_chart_absence_reason(
                evidence_sets,
                rendered_record_ids,
                question=plan.resolved_question,
            )
            if requested_chart
            else "fewer_than_two_grounded_points"
        )
        if requested_chart and chart_was_requested(plan.resolved_question) and not charts:
            detail = (
                "수신 값이 숫자형으로 해석되지 않아"
                if chart_reason == "requested_metric_values_not_numeric"
                else "비교 가능한 복수 기간 값이 부족해"
            )
            final_text = (
                f"{final_text.rstrip()}\n\n"
                f"{requested_chart} 추이 차트는 {detail} 만들 수 없습니다."
            )
        final_text = _append_file_analytics_tables(
            final_text,
            (*hira_statistics_tables, *file_analytics_tables),
        )
        trace["charts"] = {
            "generated_count": len(charts),
            "requested_metric": requested_chart,
            "reason": chart_reason,
        }
        chart_bytes = latency_probe.checkpoint(
            "runtime.charts",
            input_bytes=trace_bytes,
            output_value=charts,
            object_count=len(charts),
        )
        trace["inspection_detail"] = _attach_entity_completion_to_inspection(
            build_inspection_detail(
                plan,
                results,
                evidence_sets,
                deterministic_render,
                expansion=trace["expansion"],
                answer_text=final_text,
                used_record_ids=document_fact_ids,
            ),
            entity_completion,
        )
        inspection_bytes = latency_probe.checkpoint(
            "runtime.inspection",
            input_bytes=chart_bytes,
            output_value=trace["inspection_detail"],
            object_count=len(trace["inspection_detail"].get("calls", ())),
        )
        trace["scope_provenance_projection"] = build_scope_provenance_projection(
            evidence_sets,
            deterministic_render.nodes,
            strict=False,
        )
        provenance_bytes = latency_probe.checkpoint(
            "runtime.provenance",
            input_bytes=inspection_bytes,
            output_value=trace["scope_provenance_projection"],
        )
        sources = _answer_source_names(results)
        next_state = _derive_session_state(
            question,
            plan,
            results,
            previous=session_state,
        )
        if self._state_store is not None:
            self._state_store.save(session_id, next_state)
        state_bytes = latency_probe.checkpoint(
            "runtime.session_state",
            input_bytes=provenance_bytes,
            output_value=next_state.public_dict(),
            object_count=len(next_state.last_source_record_ids),
        )
        final_text = _append_reuse_surface(
            final_text,
            results,
            reused=bool(can_reuse_prior),
        )
        answer = V4Answer(
            text=final_text,
            charts=charts,
            sources=sources,
            trace=trace,
            timing=stage_timing,
            conversation_id=session_id,
        )
        latency_probe.checkpoint(
            "runtime.v4_answer",
            input_bytes=state_bytes,
            output_bytes=(trace_bytes or 0) + (chart_bytes or 0) + len(final_text.encode("utf-8")),
            object_count=record_count,
        )
        return answer

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


def _answer_source_names(results: Sequence[SourceResult]) -> tuple[str, ...]:
    source_names = [
        citation.source
        for result in results
        if result.status == "ok"
        for citation in result.citations
    ]
    source_names.extend(
        result.source for result in results if is_typed_absence_record(result)
    )
    return tuple(dict.fromkeys(source_names))


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


def _needs_deterministic_expansion(plan: Any, question: str) -> bool:
    lowered = question.casefold()
    return bool(
        plan.needs_second_hop
        or any(token in lowered for token in ("제네릭", "치료제", "동일 성분", "generic"))
    )


def _derive_session_state(
    question: str,
    plan: Any,
    results: Sequence[SourceResult],
    *,
    previous: SessionState | None,
) -> SessionState:
    previous = previous or SessionState()
    interpreted_question = " ".join(
        (
            question,
            plan.resolved_question,
            *plan.requested_answer_shape.entities,
        )
    )
    explicit_shape_entities = tuple(
        entity
        for raw_entity in plan.requested_answer_shape.entities
        if (entity := str(raw_entity).strip())
        and entity.casefold() in question.casefold()
    )
    planned_entities = _kcd_codes(interpreted_question)
    entities = tuple(dict.fromkeys((*explicit_shape_entities, *planned_entities)))
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
        requested_grain=_requested_grain(question),
        referenced_entity_set=referenced,
        active_filters=_active_filters(question) or (() if topic_switched else previous.active_filters),
        time_window=_time_window(interpreted_question)
        or (() if topic_switched else previous.time_window),
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
    # R71-E maps the requested axis to the source that owns it (환자수 -> hira,
    # 매출 -> mart). The uploaded file had no entry, so a file-directed question
    # fell through to whichever market token appeared next and the body was
    # ordered on a source the user did not ask for. A file reference is the most specific
    # axis a user can give, so it is resolved first; the file/market comparison
    # is excluded because that request wants both legs ordered as before.
    if has_file_axis_reference(question) and not has_explicit_file_source_comparison(question):
        return "document"
    if any(
        marker in normalized
        for marker in ("임상시험", "임상 시험", "임상", "clinical", "nct")
    ) or re.search(r"(?<![a-z0-9])trials?(?![a-z0-9])", normalized):
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


PER_TOOL_TIMEOUT_ENV = "CHAT_V4_PER_TOOL_TIMEOUT_SECONDS"
TOTAL_TIMEOUT_ENV = "CHAT_V4_TOTAL_TIMEOUT_SECONDS"

# One tool's share of the wave. Raised from 45.0: at 45 the market lane was cut
# five seconds before the wave itself ended, so an ingredient expansion could
# lose a brand to a limit that protected nothing -- the wave was going to stop
# it anyway. Above the wave budget this limit no longer fires on its own, which
# is the intent: the wave is the one thing that bounds the answer.
_DEFAULT_PER_TOOL_TIMEOUT_S = 90.0

# The wave, and with it the whole retrieval stage. Deliberately NOT raised.
# Ten measured answers put planner+synthesis+assembly at up to 77.4 s, and an
# earlier round observed synthesis alone at 69.5 s. Against the 130 s ceiling
# this leaves the wave 43.5-52.6 s, so 50.0 is already at the top of its band
# and raising it would buy retrieval time by spending the answer's deadline.
_DEFAULT_TOTAL_TIMEOUT_S = 50.0


def per_tool_timeout_s() -> float:
    return _timeout_from_env(PER_TOOL_TIMEOUT_ENV, _DEFAULT_PER_TOOL_TIMEOUT_S)


def total_timeout_s() -> float:
    """The single wave budget.

    Read here by both the executor's own cap and the first wave's call-site
    clamp. They used to be two independent copies of ``50.0``, so raising the
    environment override moved one and the other silently clamped it back:
    the setting could only ever lower the budget. One reader, one value.
    """
    return _timeout_from_env(TOTAL_TIMEOUT_ENV, _DEFAULT_TOTAL_TIMEOUT_S)


def _timeout_from_env(name: str, default: float) -> float:
    """Read a retrieval budget from the environment without changing its value.

    The budgets used to be literals here, so tuning one meant shipping code.
    The defaults are the values that were hardcoded; a malformed or
    non-positive override is reported and ignored rather than silently applied.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default
    if value <= 0:
        LOGGER.warning("ignoring non-positive %s=%r; using %s", name, raw, default)
        return default
    return value


def build_default_runtime() -> V4Runtime:
    from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies

    dependencies = build_chat_agent_dependencies(external_mode="live")
    return V4Runtime(
        planner=V4Planner(planner_client()),
        executor=ParallelSourceExecutor(
            adapters=build_source_adapters(dependencies=dependencies),
            per_tool_timeout_s=per_tool_timeout_s(),
            total_timeout_s=total_timeout_s(),
        ),
        synthesizer=V4Synthesizer(synthesizer_client()),
        state_store=SessionStateStore.from_env(),
        entity_resolver=dependencies.resolver,
        disease_code_client=dependencies.external,
    )


def _synthesis_failure_outcome(exc: Exception) -> SynthesisOutcome:
    """Invariant 3: a failed commentary stage must not cost the grounded facts.

    The exception is logged with its traceback and its type is carried in the
    trace so the inspection panel still reports what went wrong. Only the
    user-facing surface is kept free of internal names.
    """
    LOGGER.exception("v4 synthesis step failed; answering from deterministic facts only")
    return SynthesisOutcome(
        text="해설은 생성하지 못했고 조회 결과만 표시합니다.",
        trace={
            "status": "fallback",
            "fallback_reason": "synthesis_step_failed",
            "error_type": type(exc).__name__,
            "partial_generated": False,
        },
    )


def _remaining(deadline: float) -> float:
    return max(0.1, deadline - time.monotonic())


def _should_retry_post_semantic_insight(
    *,
    synthesis_trace: Mapping[str, Any],
    post_semantic_trace: Mapping[str, Any],
    remaining_s: float,
) -> bool:
    """Reserve the single S17 repair for the final evidence-binding failure."""
    retry_trace = synthesis_trace.get("insight_richness_retry")
    prior_attempted = bool(
        isinstance(retry_trace, Mapping) and retry_trace.get("attempted")
    )
    contract_repair_needed = bool(
        not post_semantic_trace.get("contract_met")
        and post_semantic_trace.get("reason_code")
        in {"MISSING_REQUIRED_ROLE", "MISSING_EVIDENCE", "AXIS_UNCLOSED", "융합 추론 누락"}
    )
    expansion_repair_needed = bool(
        post_semantic_trace.get("data_rich")
        and not post_semantic_trace.get("expansion_target_met", True)
        and post_semantic_trace.get("expansion_retry_reason") == "확장 부족"
    )
    return bool(
        not prior_attempted
        and remaining_s > 1.0
        and (contract_repair_needed or expansion_repair_needed)
    )


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


def _partial_axis_streaming_body(
    plan: PlannerOutput,
    question: str,
    source: SourceName,
    results: tuple[SourceResult, ...],
    *,
    source_render_limit: int,
    observed_on: date | None = None,
) -> str:
    """Render one completed core axis without waiting for unrelated lanes."""
    usable_results = tuple(
        result
        for result in results
        if result.source == source
        and _result_exclusion_reason(result) is None
        and _payload_has_content(result.payload)
    )
    if not usable_results:
        return ""
    partial_plan = plan.model_copy(update={"answer_sources": (source,)})
    partial_lane_execution = {
        source: build_lane_execution_records(partial_plan, usable_results)[source]
    }
    partial_question = _partial_axis_contract_question(question, source)
    try:
        _evidence_sets, deterministic_render = build_lossless_render(
            partial_plan,
            usable_results,
            observed_on=observed_on or current_kst_date(),
            source_render_limit=source_render_limit,
            lane_execution=partial_lane_execution,
        )
        return compose_streaming_body(
            deterministic_render,
            mode=configured_lossless_mode(),
            requested_fields_mode=configured_requested_fields_mode(),
            request_satisfaction_mode=configured_request_satisfaction_mode(),
            question=_layout_axis_question(partial_question, partial_plan),
        )
    except LosslessInvariantError:
        LOGGER.exception("v4 partial axis lossless invariant failed")
        raise
    except Exception:
        LOGGER.exception("v4 partial axis stream build failed")
        return ""


def _partial_axis_stream_is_safe(
    question: str,
    answer_sources: Sequence[str],
) -> bool:
    """Do not commit an incomplete core when requested axes have separate owners."""

    return (
        len(question_axes(question)) <= 1
        or len(tuple(dict.fromkeys(answer_sources))) <= 1
    )


def _partial_axis_contract_question(question: str, source: SourceName) -> str:
    """Keep an early stream on the user's requested metric for one completed lane."""

    current = " ".join(str(question or "").split())
    if len(_layout_axis_groups(current)) <= 1:
        return current

    source_terms: dict[str, tuple[str, ...]] = {
        "hira": ("환자수", "환자 수", "청구 실인원", "유병률", "급여"),
        "mart": ("매출", "실적", "총액", "점유율", "sellout", "sell out"),
        "patent": ("특허", "만료", "재심사"),
        "clinicaltrials": ("임상", "clinical", "nct", "trial"),
        "nedrug": ("허가", "품목"),
        "openfda": ("부작용", "이상사례", "안전성", "fda"),
        "web": ("뉴스", "보도", "웹"),
    }
    terms = source_terms.get(source)
    if not terms or source in {"document_rag", "document_sql"}:
        return current

    clauses = tuple(
        clause.strip(" ,")
        for clause in re.split(r"\s*(?:이랑|랑|와|과|및|,)\s*", current)
        if clause.strip(" ,")
    )
    matching = tuple(
        clause
        for clause in clauses
        if any(term in clause.casefold() for term in terms)
    )
    if len(matching) == 1:
        return matching[0]

    labels: list[tuple[int, str]] = []
    normalized = current.casefold()
    for term in terms:
        position = normalized.find(term)
        if position >= 0:
            label = "환자수" if term in {"환자 수", "청구 실인원"} else term
            labels.append((position, label))
    selected = tuple(dict.fromkeys(label for _position, label in sorted(labels)))
    return " ".join(selected) or current


def _payload_has_content(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, (str, bytes, tuple, list, dict, set)):
        return bool(payload)
    return True


def _execution_plan(
    executor: Any,
    plan: Any,
    *,
    clinical_query_anchor: str,
) -> tuple[Any, dict[str, Any]]:
    fanned_plan = (
        plan
        if getattr(plan, "query_scope", None) is not None
        else fan_out_tier_zero_queries(plan)
    )
    prepare_plan = getattr(executor, "prepare_plan", None)
    if not callable(prepare_plan):
        executable = apply_source_call_cap(fanned_plan)
        return executable, _clinical_normalization_trace(plan, executable)
    prepared = prepare_plan(fanned_plan, clinical_query_anchor=clinical_query_anchor)
    executable = apply_source_call_cap(prepared)
    return executable, _clinical_normalization_trace(plan, executable)


def _linked_clinical_query_anchor(first_question: str, linked_question: str) -> str:
    if clinical_scope_suffix(linked_question):
        return linked_question
    inherited_scope = clinical_scope_suffix(first_question)
    if not inherited_scope:
        return linked_question
    return f"{linked_question} {inherited_scope}"


def _exclude_first_hop_queries(first_plan: Any, linked_plan: Any) -> Any:
    """Prevent a second-hop wave from re-executing first-hop source/query pairs."""
    retained_sources: list[str] = []
    query_updates: dict[str, tuple[str, ...]] = {}
    for source in linked_plan.answer_sources:
        if source not in SOURCE_NAMES:
            retained_sources.append(source)
            continue
        first_queries = {
            " ".join(query.split()).casefold()
            for query in getattr(first_plan.tool_queries, source)
        }
        linked_queries = tuple(
            query
            for query in getattr(linked_plan.tool_queries, source)
            if " ".join(query.split()).casefold() not in first_queries
        )
        query_updates[source] = linked_queries
        if not linked_queries:
            continue
        retained_sources.append(source)
    return linked_plan.model_copy(
        update={
            "answer_sources": tuple(retained_sources),
            "tool_queries": linked_plan.tool_queries.model_copy(update=query_updates),
        }
    )


def _deterministic_clinical_query_anchor(
    question: str,
    state: SessionState | None,
) -> str:
    normalized = " ".join(question.split())
    if state is None or not _should_inherit_session_contract(question, state):
        return normalized
    return _append_missing_constraints(
        normalized,
        _session_query_constraints(state, question),
    )


def _clinical_normalization_trace(
    planner_plan: Any | None,
    execution_plan: Any | None,
) -> dict[str, Any]:
    planner_queries = (
        tuple(planner_plan.tool_queries.clinicaltrials)
        if planner_plan is not None
        else ()
    )
    execution_queries = (
        tuple(execution_plan.tool_queries.clinicaltrials)
        if execution_plan is not None
        else ()
    )
    execution_concepts = (
        tuple(execution_plan.clinical_query_specs)
        if execution_plan is not None
        else ()
    )
    return {
        "applied": planner_queries != execution_queries,
        "planner_queries": list(planner_queries),
        "execution_queries": list(execution_queries),
        "execution_concepts": [
            concept.model_dump(mode="json") for concept in execution_concepts
        ],
    }


def _claim_ir_shadow_enabled() -> bool:
    value = os.environ.get("CHAT_CLAIM_IR_SHADOW", "true").strip().casefold()
    return value not in {"0", "false", "off", "disabled", "no"}


def _result_entity_id(plan: Any, result: SourceResult) -> str | None:
    entities = tuple(getattr(plan.requested_answer_shape, "entities", ()))
    matched = [
        entity
        for entity in sorted(entities, key=len, reverse=True)
        if entity.casefold() in result.query.casefold()
    ]
    return matched[0] if len(matched) == 1 else None


def _inject_entity_completion_surface(
    answer: str,
    completion: Any,
    *,
    current_question: str = "",
    results: Sequence[SourceResult] = (),
) -> tuple[str, dict[str, Any]]:
    rows = tuple(completion.rows)
    entity_types = {
        str(row.get("entity")): str(row.get("entity_type"))
        for row in getattr(completion, "entity_types", ())
    }
    normalized_question = current_question.casefold()
    eligible_rows = tuple(
        row
        for row in rows
        if not current_question
        or entity_types.get(str(row["entity"])) in {"질환", "상병코드"}
        or str(row["entity"]).casefold() in normalized_question
    )
    present_in_answer = [
        str(row["entity"])
        for row in eligible_rows
        if _entity_present_in_answer(str(row["entity"]), answer)
    ]
    record_satisfaction_sources = {
        str(row["entity"]): list(
            dict.fromkeys(
                result.source
                for result in results
                if result.status == "ok"
                and isinstance(result.payload, Mapping)
                and isinstance(result.payload.get("records"), list)
                and bool(result.payload["records"])
                and _entity_present_in_answer(str(row["entity"]), result.query)
            )
        )
        for row in eligible_rows
        if row["status"] != "COMPLETE"
    }
    record_satisfaction_sources = {
        entity: sources
        for entity, sources in record_satisfaction_sources.items()
        if sources
    }
    record_satisfied_entities = list(record_satisfaction_sources)
    missing_entities = [
        str(row["entity"])
        for row in eligible_rows
        if row["status"] != "COMPLETE"
        and str(row["entity"]) not in present_in_answer
        and str(row["entity"]) not in record_satisfied_entities
    ]
    confirmed_entities = [
        str(row["entity"])
        for row in eligible_rows
        if row["status"] == "COMPLETE"
        or str(row["entity"]) in present_in_answer
        or str(row["entity"]) in record_satisfied_entities
    ]
    incomplete_count = len(missing_entities)
    base_trace = {
        "row_count": len(rows),
        "eligible_row_count": len(eligible_rows),
        "present_in_answer": present_in_answer,
        "record_satisfied_entities": record_satisfied_entities,
        "record_satisfaction_sources": record_satisfaction_sources,
        "notice_entities": missing_entities,
        "table_location": "inspection",
    }
    if len(eligible_rows) < 2 or incomplete_count == 0:
        return answer, {
            **base_trace,
            "injected": False,
        }
    missing_text = "·".join(missing_entities)
    confirmed_text = "·".join(confirmed_entities)
    entity_type_set = {entity_types.get(entity) for entity in missing_entities + confirmed_entities}
    label = (
        "상병코드·질환 항목"
        if entity_type_set and entity_type_set <= {"질환", "상병코드"}
        else "조회 대상"
    )
    block = (
        f"확인된 {len(confirmed_entities)}개 {label}({confirmed_text}) 기준으로 비교했으며, "
        f"{missing_text}은 조회 결과와 연결하지 못했습니다."
        if confirmed_entities and missing_entities
        else ""
    )
    if not block:
        return answer, {
            **base_trace,
            "injected": False,
            "incomplete_count": incomplete_count,
        }
    insertion = re.search(
        r"(?m)^##\s+(?:근거와 맥락|근거|종합 인사이트|해석 상한|미확인 요소|출처)\s*$",
        answer,
    )
    if insertion is None:
        updated = f"{answer.rstrip()}\n\n{block}".strip()
    else:
        updated = (
            f"{answer[:insertion.start()].rstrip()}\n\n{block}\n\n"
            f"{answer[insertion.start():].lstrip()}"
        ).strip()
    return updated, {
        **base_trace,
        "injected": True,
        "incomplete_count": incomplete_count,
    }


def _entity_present_in_answer(entity: str, answer: str) -> bool:
    if not entity:
        return False
    if re.fullmatch(r"[A-Za-z0-9._-]+", entity):
        return re.search(
            rf"(?<![A-Za-z0-9._-]){re.escape(entity)}(?![A-Za-z0-9._-])",
            answer,
            re.IGNORECASE,
        ) is not None
    return entity.casefold() in answer.casefold()


def _attach_entity_completion_to_inspection(
    detail: Mapping[str, Any],
    completion: Any,
) -> dict[str, Any]:
    return {
        **detail,
        "entity_completion": {
            "rows": list(completion.rows),
            "entity_types": list(completion.entity_types),
            "scope_notice": completion.scope_notice,
            "table_location": "inspection",
        },
    }


def _layout_axis_question(
    question: str,
    plan: Any,
    *,
    prefer_document: bool = False,
) -> str:
    current = " ".join(str(question or "").split())
    if prefer_document:
        return f"{current} 업로드 문서".strip()
    if _record_type(current) is not None:
        return current
    requested_attributes = [
        str(value).strip()
        for value in plan.requested_answer_shape.measure_or_attribute
        if str(value).strip()
    ]
    patient_attributes = {"patient_count", "환자수"}
    prevalence_attributes = {"prevalence", "유병률"}
    if patient_attributes.intersection(requested_attributes) and prevalence_attributes.intersection(
        requested_attributes
    ):
        resolved = " ".join(str(getattr(plan, "resolved_question", "") or "").split())
        resolved_has_patient_count = "환자" in resolved
        resolved_has_prevalence = "유병률" in resolved
        if resolved_has_patient_count and not resolved_has_prevalence:
            requested_attributes = [
                value for value in requested_attributes if value not in prevalence_attributes
            ]
        elif resolved_has_prevalence and not resolved_has_patient_count:
            requested_attributes = [
                value for value in requested_attributes if value not in patient_attributes
            ]
    attributes = " ".join(
        _RECORD_TYPE_QUERY_LABELS.get(value, value) for value in requested_attributes
    )
    return " ".join(value for value in (current, attributes) if value)


def _surface_contract_question(question: str, plan: Any) -> str:
    current = _layout_axis_question(question, plan)
    normalized = " ".join(str(question or "").casefold().split())
    if not any(
        marker in normalized
        for marker in ("그거", "그것", "아까", "이전", "앞서", "그 중", "그중")
    ):
        return current

    current_axes = set(_layout_axis_groups(current))
    additions: list[str] = []
    for raw_value in plan.requested_answer_shape.measure_or_attribute:
        value = str(raw_value).strip()
        if not value:
            continue
        label = _RECORD_TYPE_QUERY_LABELS.get(value, value)
        axis = _LAYOUT_AXIS_BY_RECORD_TYPE.get(value) or next(
            iter(_layout_axis_groups(label)),
            None,
        )
        if axis is None or axis in current_axes:
            continue
        additions.append(
            _LAYOUT_AXIS_LABEL_BY_ATTRIBUTE.get(
                value,
                _LAYOUT_AXIS_CANONICAL_LABELS.get(axis, label),
            )
        )
        current_axes.add(axis)
    return " ".join((current, *additions)).strip()


def _layout_axis_groups(value: str) -> tuple[str, ...]:
    normalized = " ".join(value.casefold().split())
    return tuple(
        axis
        for axis, terms in (
            ("patient", ("환자", "유병률")),
            ("market", ("매출", "총액", "시장 지표", "sellout", "sell out")),
            ("patent", ("특허", "만료", "재심사")),
            ("clinical", ("임상", "clinical", "nct")),
            ("reimbursement", ("급여",)),
            ("approval", ("허가", "품목")),
            ("safety", ("부작용", "안전성")),
        )
        if any(term in normalized for term in terms)
    )


def _gate_deletion_rate(before: str, after: str) -> float:
    before_count = len(tuple(_surface_sentences(before)))
    after_count = len(tuple(_surface_sentences(after)))
    if before_count == 0:
        return 0.0
    return round(max(0, before_count - after_count) / before_count, 6)


def _surface_sentences(value: str) -> tuple[str, ...]:
    prose = " ".join(
        line.strip()
        for line in value.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "|", "```"))
    )
    return tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?。])\s+", prose)
        if sentence.strip()
    )


def _soft_deadline_exempt_sources(question: str) -> tuple[SourceName, ...]:
    normalized = question.casefold()
    clinical_requested = any(
        token in normalized for token in ("임상", "clinical", "nct")
    )
    patent_requested = any(
        token in normalized for token in ("특허", "오렌지북", "orange book")
    ) or (clinical_requested and "제네릭" in normalized)
    output: list[SourceName] = []
    if clinical_requested:
        output.append("clinicaltrials")
    if patent_requested:
        output.append("patent")
    return tuple(output)


def _empty_usage() -> dict[str, str]:
    return {
        "input_tokens": "not_applicable",
        "output_tokens": "not_applicable",
        "thinking_tokens": "not_applicable",
        "text_tokens": "not_applicable",
        "thinking_level": "not_applicable",
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
    # thinking_level rides along because output tokens are what synthesis time is
    # made of -- 6.5 ms each, measured flat across every duration band -- and
    # most of them are reasoning tokens. Without the level that was asked for,
    # a drop in reasoning tokens cannot be told apart from ordinary LLM variance.
    thinking = trace.get("thinking")
    thinking = thinking if isinstance(thinking, Mapping) else {}
    return {
        "input_tokens": _int_or_zero(usage.get("prompt_tokens")),
        "output_tokens": _int_or_zero(usage.get("completion_tokens")),
        "thinking_tokens": _int_or_zero(details.get("reasoning_tokens")),
        "text_tokens": _int_or_zero(details.get("text_tokens")),
        "thinking_level": str(thinking.get("requested_level") or "not_reported"),
        "finish_reason": str(trace.get("finish_reason") or "not_reported"),
        "measurement": "reported",
    }


def _int_or_zero(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _normalized_planner_usage(
    value: object, thinking: object = None
) -> dict[str, int | str]:
    if not isinstance(value, Mapping) or not value:
        return _empty_usage()
    thinking = thinking if isinstance(thinking, Mapping) else {}
    return {
        "input_tokens": _int_or_zero(value.get("input_tokens")),
        "output_tokens": _int_or_zero(value.get("output_tokens")),
        "thinking_tokens": _int_or_zero(value.get("thinking_tokens")),
        "text_tokens": _int_or_zero(value.get("text_tokens")),
        "thinking_level": str(thinking.get("requested_level") or "not_reported"),
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
    visible = [value.strip() for value in intents if value.strip()]
    detail = "\n".join(f"- {value}" for value in visible)
    if not detail:
        return "질문에 맞는 조회 경로를 구성했습니다"
    return detail


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
    if not queries:
        return "조회 내용 확인 불가"
    return "\n".join(_public_progress_query(query) for query in queries)


_MIXED_AXIS_CONNECTOR_RE = re.compile(
    r"(?:이랑|랑|와|과|및|함께|같이|동시에|\+)",
    re.IGNORECASE,
)
_EXPLICIT_NON_FILE_SOURCE_RE = re.compile(
    r"(?:"
    r"(?<![a-z0-9])(?:hira|ubist|iqvia|openfda|clinicaltrials(?:\.gov)?|kipris)(?![a-z0-9])"
    r"|심평원|건강보험심사평가원|클리니컬스"
    r"|(?:mart|마트|시장\s*데이터|내부\s*데이터|자사\s*데이터)에서"
    r"|(?:식약처|의약품안전나라|특허목록|웹|뉴스)(?:에서|로|검색)"
    r")",
    re.IGNORECASE,
)
_FILE_SESSION_AXIS_SOURCES = {
    "patient_statistics": "hira",
    "market_total": "mart",
    "patent": "patent",
    "clinical_trials": "clinicaltrials",
    "regulatory": "nedrug",
    "safety": "openfda",
}
_FILE_SESSION_EXPLICIT_SOURCE_PATTERNS = {
    "mart": re.compile(
        r"(?<![a-z0-9])(?:ubist|iqvia|mart)(?![a-z0-9])|"
        r"(?:내부\s*데이터|시장\s*데이터|자사\s*데이터|데이터마트|마트)(?:에서|로|와|과|기준)",
        re.IGNORECASE,
    ),
    "hira": re.compile(
        r"(?<![a-z0-9])hira(?![a-z0-9])|심평원|건강보험심사평가원",
        re.IGNORECASE,
    ),
    "nedrug": re.compile(r"의약품안전나라|허가\s*(?:정보|현황|자료)", re.IGNORECASE),
    "openfda": re.compile(
        r"(?<![a-z0-9])(?:openfda|fda)(?![a-z0-9])|부작용|이상사례|안전성",
        re.IGNORECASE,
    ),
    "clinicaltrials": re.compile(
        r"clinical\s*trials?(?:\.gov)?|클리니컬스|임상\s*(?:시험|현황|자료)",
        re.IGNORECASE,
    ),
    "web": re.compile(r"(?:웹|뉴스|보도)(?:에서|로|검색|기사)", re.IGNORECASE),
    "patent": re.compile(r"특허|patent", re.IGNORECASE),
}
_FILE_SESSION_INTENT_LABELS = {
    "mart": "내부 데이터마트에서 매출·시장 근거 확인",
    "nedrug": "의약품안전나라에서 허가 근거 확인",
    "hira": "건강보험심사평가원에서 질환 통계 근거 확인",
    "openfda": "FDA에서 안전성 근거 확인",
    "clinicaltrials": "ClinicalTrials.gov에서 임상 근거 확인",
    "web": "공개 웹에서 명시 요청 근거 확인",
    "patent": "식약처 의약품 특허목록에서 특허 근거 확인",
}


def _force_all_source_plan(
    plan: PlannerOutput,
    *,
    question: str,
    include_document: bool,
    resolver: Any | None = None,
    molecule_fallback: Any | None = None,
) -> tuple[PlannerOutput, dict[str, Any]]:
    """Make execution fan out to every external lane for every question."""

    composed = compose_all_source_queries(
        plan,
        question,
        resolver=resolver,
        molecule_fallback=molecule_fallback,
    )
    plan = composed.plan
    query_updates: dict[str, tuple[str, ...]] = {}
    requested_calls: dict[str, int] = {}
    executed_calls: dict[str, int] = {}
    unexecuted_reasons: dict[str, str] = {}
    for source in SOURCE_NAMES:
        existing = tuple(
            dict.fromkeys(
                query.strip()
                for query in getattr(plan.tool_queries, source)
                if query.strip()
            )
        )
        query_updates[source] = existing
        requested_calls[source] = max(1, len(existing))
        executed_calls[source] = len(existing)
        if not existing:
            unexecuted_reasons[source] = "query_construction_unavailable"

    answer_sources = tuple(
        dict.fromkeys(
            (("document",) if include_document else ()) + tuple(SOURCE_NAMES)
        )
    )
    intents = tuple(
        dict.fromkeys(
            (
                *plan.expanded_intents,
                *(_FILE_SESSION_INTENT_LABELS[source] for source in SOURCE_NAMES),
            )
        )
    )
    forced = plan.model_copy(
        update={
            "answer_sources": answer_sources,
            "expanded_intents": intents,
            "tool_queries": plan.tool_queries.model_copy(update=query_updates),
            "query_scope": QueryScope(
                requested_calls=requested_calls,
                executed_calls=executed_calls,
                omitted_queries={},
                unexecuted_reasons=unexecuted_reasons,
            ),
        }
    )
    return forced, {
        "applied": True,
        "policy": "standing_pl_p1_all_tools",
        "answer_sources": list(answer_sources),
        "executed_calls": executed_calls,
        **composed.trace,
    }


def _bind_active_file_session_plan(
    plan: PlannerOutput,
    *,
    question: str,
    supplemental_results: Sequence[SourceResult],
    resolver: Any | None = None,
) -> tuple[PlannerOutput, dict[str, Any]]:
    """Make displayed and executed file-session lanes share one decision."""

    if not any(result.source == "document" for result in supplemental_results):
        return plan, {"applied": False, "reason": "no_active_document"}

    document_answer_eligible = bool(
        is_document_overview_question(question)
        or has_file_axis_reference(question)
        or any(
            result.source == "document"
            and isinstance(result.payload, Mapping)
            and result.payload.get("answer_eligible") is True
            for result in supplemental_results
        )
    )
    axes = set(question_axes(question))
    allowed = {
        source
        for axis, source in _FILE_SESSION_AXIS_SOURCES.items()
        if axis in axes
    }
    normalized_question = " ".join(question.split())
    allowed.update(explicit_file_comparison_sources(normalized_question))
    allowed.update(
        source
        for source, pattern in _FILE_SESSION_EXPLICIT_SOURCE_PATTERNS.items()
        if pattern.search(normalized_question)
    )

    # A spreadsheet total is owned by the document SQL lane.  The mart leg is
    # admitted only when the user names a market source or a resolvable brand.
    if "mart" in allowed and not (
        _question_has_resolved_brand(normalized_question, resolver)
        or "mart" in explicit_file_comparison_sources(normalized_question)
        or _FILE_SESSION_EXPLICIT_SOURCE_PATTERNS["mart"].search(normalized_question)
    ):
        allowed.discard("mart")
    # Patent news is redundant once the official patent lane is in the plan.
    if "patent" in allowed:
        allowed.discard("web")

    # Standing PL P1: a file-scoped question still executes every external
    # source. Relevance is decided after retrieval, never during fan-out.
    allowed = set(SOURCE_NAMES)

    ordered_allowed = tuple(source for source in SOURCE_NAMES if source in allowed)
    query_updates: dict[str, tuple[str, ...]] = {}
    requested_calls: dict[str, int] = {}
    executed_calls: dict[str, int] = {}
    omitted_queries: dict[str, tuple[str, ...]] = {}
    unexecuted_reasons: dict[str, str] = {}
    discarded: dict[str, list[str]] = {}
    previous_scope = plan.query_scope
    for source, raw_queries in plan.tool_queries.items():
        queries = tuple(dict.fromkeys(query for query in raw_queries if query.strip()))
        if source in allowed:
            selected = queries or (normalized_question,)
            previous_requested = (
                int(previous_scope.requested_calls.get(source, 0))
                if previous_scope is not None
                else 0
            )
            requested_calls[source] = max(previous_requested, len(selected))
            executed_calls[source] = len(selected)
            if previous_scope is not None:
                previous_omitted = tuple(previous_scope.omitted_queries.get(source, ()))
                if previous_omitted:
                    omitted_queries[source] = previous_omitted
        else:
            selected = ()
            requested_calls[source] = 0
            executed_calls[source] = 0
            if queries:
                discarded[source] = list(queries)
        query_updates[source] = selected

    document_name = _file_only_document_name(supplemental_results)
    document_intent = (
        "업로드 문서 본문 요약"
        if is_document_overview_question(normalized_question)
        else "업로드 문서에서 질문 근거 확인"
    )
    expanded_intents = (
        document_intent,
        *(_FILE_SESSION_INTENT_LABELS[source] for source in ordered_allowed),
    )
    resolved_question = normalized_question or plan.resolved_question
    if is_document_overview_question(normalized_question):
        resolved_question = (
            f"업로드 문서({document_name}) 요약 요청"
            if document_name
            else "업로드 문서 요약 요청"
        )

    answer_sources = (
        (("document",) if document_answer_eligible else ()) + ordered_allowed
    )
    bound = plan.model_copy(
        update={
            "resolved_question": resolved_question,
            "expanded_intents": expanded_intents,
            "answer_sources": answer_sources,
            "tool_queries": plan.tool_queries.model_copy(update=query_updates),
            "clinical_query_specs": (
                plan.clinical_query_specs if "clinicaltrials" in allowed else ()
            ),
            "needs_second_hop": False,
            "query_scope": QueryScope(
                requested_calls=requested_calls,
                executed_calls=executed_calls,
                omitted_queries=omitted_queries,
                unexecuted_reasons=unexecuted_reasons,
            ),
        }
    )
    return bound, {
        "applied": True,
        "reason": "active_file_session_single_rule",
        "question_axes": sorted(axes),
        "document_answer_eligible": document_answer_eligible,
        "answer_sources": list(answer_sources),
        "allowed_external_sources": list(ordered_allowed),
        "discarded_planner_queries": discarded,
        "executed_calls": executed_calls,
        "displayed_intents": list(expanded_intents),
    }


def _is_file_only_request(
    question: str,
    supplemental_results: Sequence[SourceResult],
    *,
    resolver: Any | None = None,
) -> bool:
    has_document = any(result.source == "document" for result in supplemental_results)
    if not has_document:
        return False
    explicit_file_request = bool(
        is_document_overview_question(question) or has_file_axis_reference(question)
    )
    document_relevant = any(
        result.source == "document"
        and isinstance(result.payload, Mapping)
        and result.payload.get("answer_eligible") is True
        for result in supplemental_results
    )
    if not explicit_file_request and not document_relevant:
        return False
    if has_explicit_file_source_comparison(question):
        return False
    axes = set(question_axes(question))
    has_resolved_brand = _question_has_resolved_brand(question, resolver)
    if _EXPLICIT_NON_FILE_SOURCE_RE.search(question):
        return False
    if explicit_file_request:
        mixed_connector = bool(_MIXED_AXIS_CONNECTOR_RE.search(question))
        return not (
            mixed_connector
            and (
                len(axes) > 1
                or ("market_total" in axes and has_resolved_brand)
            )
        )
    return not (has_resolved_brand and bool(axes))


def _question_has_resolved_brand(question: str, resolver: Any | None) -> bool:
    if resolver is None:
        return False
    try:
        return bool(resolver.resolve_many(question, allow_default=False))
    except (LookupError, OSError, TimeoutError):
        return False


def _bind_file_only_request_plan(
    plan: PlannerOutput,
    *,
    question: str = "",
    supplemental_results: Sequence[SourceResult] = (),
) -> PlannerOutput:
    zero_calls = {source: 0 for source in SOURCE_NAMES}
    omitted = {
        source: tuple(getattr(plan.tool_queries, source)) for source in SOURCE_NAMES
    }
    display_updates: dict[str, Any] = {}
    if is_document_overview_question(question):
        document_name = _file_only_document_name(supplemental_results)
        display_updates = {
            "resolved_question": (
                f"업로드 문서({document_name}) 요약 요청"
                if document_name
                else "업로드 문서 요약 요청"
            ),
            "expanded_intents": ("업로드 문서 본문 요약",),
        }
    return plan.model_copy(
        update={
            **display_updates,
            "answer_sources": ("document",),
            "needs_second_hop": False,
            "query_scope": QueryScope(
                requested_calls=zero_calls,
                executed_calls=zero_calls,
                omitted_queries=omitted,
                unexecuted_reasons={
                    source: "file_only_request" for source in SOURCE_NAMES
                },
            ),
        }
    )


def _file_only_document_name(
    supplemental_results: Sequence[SourceResult],
) -> str:
    for result in supplemental_results:
        if result.source != "document" or not isinstance(result.payload, Mapping):
            continue
        names = result.payload.get("document_names")
        if isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
            for value in names:
                name = " ".join(str(value or "").split())
                if name:
                    return name
        records = result.payload.get("records")
        if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                name = " ".join(
                    str(record.get("document_name") or record.get("file_name") or "").split()
                )
                if name:
                    return name
    return ""


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


def _bind_always_on_mart_query(
    plan: Any,
    question: str,
    *,
    supplemental_results: Sequence[SourceResult] = (),
) -> Any:
    """Keep the always-on mart package bound to the user's requested entity."""

    mart_query = question.strip() or plan.resolved_question
    if _is_prior_result_reference(question):
        mart_query = plan.resolved_question
    if (
        "mart" in explicit_file_comparison_sources(question)
        and _has_chso_sellout_document(supplemental_results)
    ):
        period = _korean_month_period(question)
        mart_query = " ".join(
            part for part in (period, "CSD Channel sellout 총액 알려줘") if part
        )
    queries = plan.tool_queries.model_copy(update={"mart": (mart_query,)})
    return plan.model_copy(update={"tool_queries": queries})


def _bind_file_source_comparison_queries(plan: Any, question: str) -> Any:
    sources = explicit_file_comparison_sources(question)
    if not sources:
        return plan
    plan = plan.model_copy(
        update={
            "resolved_question": question.strip() or plan.resolved_question,
            "answer_sources": ("document", *sources),
        }
    )
    if "hira" not in sources:
        return plan
    match = re.search(
        r"(?:건강보험심사평가원|심평원|HIRA)\s*(?P<query>.+?)"
        r"(?:를|을)?\s*(?:비교|대조|같(?:은|나)|알려)",
        question,
        re.IGNORECASE,
    )
    hira_query = match.group("query").strip() if match else question.strip()
    hira_query = re.sub(
        r"(?:와|과)\s+(?:(?:이|그|해당|업로드한)\s*)?"
        r"(?:파일|문서|팩트시트|리포트|엑셀|시트).*$",
        "",
        hira_query,
        flags=re.IGNORECASE,
    ).strip()
    if not hira_query.endswith(("알려줘", "알려주세요")):
        hira_query = f"{hira_query} 알려줘"
    queries = plan.tool_queries.model_copy(update={"hira": (hira_query,)})
    return plan.model_copy(update={"tool_queries": queries})


def _reconcile_answer_contract(plan: Any, question: str) -> Any:
    """Rebuild the semantic contract after deterministic runtime plan shaping."""
    contract = merge_interpretation_contract(
        derive_answer_contract(
            question,
            plan.requested_answer_shape,
            answer_sources=plan.answer_sources,
        ),
        plan.answer_contract,
        question,
    )
    return plan.model_copy(update={"answer_contract": contract})


def _bind_mixed_axis_answer_sources(
    plan: Any,
    question: str,
    *,
    supplemental_results: Sequence[SourceResult] = (),
) -> Any:
    axes = set(question_axes(question))
    if not {"patient_statistics", "market_total"}.issubset(axes):
        return plan
    if not _korean_month_period(question) or not _has_file_sql_result(
        supplemental_results
    ):
        return plan
    answer_sources = tuple(
        dict.fromkeys(
            "document" if source == "mart" else source
            for source in plan.answer_sources
        )
    )
    return plan.model_copy(update={"answer_sources": answer_sources})


def _has_file_sql_result(results: Sequence[SourceResult]) -> bool:
    for result in results:
        if result.source != "document" or not isinstance(result.payload, Mapping):
            continue
        records = result.payload.get("records")
        if not isinstance(records, (list, tuple)):
            continue
        if any(
            isinstance(record, Mapping) and file_lane_id(record) == "file_sql"
            for record in records
        ):
            return True
    return False


def _has_chso_sellout_document(results: Sequence[SourceResult]) -> bool:
    for result in results:
        if result.source != "document":
            continue
        payload_text = str(result.payload).casefold()
        if "chso" in payload_text and "sellout" in re.sub(r"\s+", "", payload_text):
            return True
    return False


def _korean_month_period(question: str) -> str:
    match = re.search(r"(20\d{2})\s*년\s*(0?[1-9]|1[0-2])\s*월", question)
    if match:
        return f"{match.group(1)}년 {int(match.group(2))}월"
    match = re.search(r"(20\d{2})[-./](0?[1-9]|1[0-2])", question)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return ""


_RECORD_TYPE_QUERY_LABELS = {
    "clinical_trial": "임상시험",
    "patent": "특허",
    "reimbursement": "급여기준",
    "approval": "허가",
    "patient_count": "환자수",
    "prevalence": "유병률",
    "market_metric": "시장 지표",
    "safety": "안전성",
    "label": "허가사항",
}
_LAYOUT_AXIS_BY_RECORD_TYPE = {
    "clinical_trial": "clinical",
    "patent": "patent",
    "reimbursement": "reimbursement",
    "approval": "approval",
    "patient_count": "patient",
    "prevalence": "patient",
    "market_metric": "market",
    "sales": "market",
    "market_share": "market",
    "safety": "safety",
    "label": "approval",
}
_LAYOUT_AXIS_LABEL_BY_ATTRIBUTE = {
    "sales": "매출",
    "market_share": "점유율",
}
_LAYOUT_AXIS_CANONICAL_LABELS = {
    "clinical": "임상시험",
    "patent": "특허",
    "reimbursement": "급여기준",
    "approval": "허가",
    "patient": "환자수",
    "market": "매출",
    "safety": "안전성",
}
_PRIMARY_SOURCE_BY_RECORD_TYPE = {
    "clinical_trial": "clinicaltrials",
    "patent": "patent",
    "reimbursement": "hira",
    "approval": "nedrug",
    "patient_count": "hira",
    "market_metric": "mart",
    "safety": "openfda",
    "label": "nedrug",
    "document": "document",
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
    plan_updates: dict[str, Any] = {
        "resolved_question": resolved_question,
        "tool_queries": plan.tool_queries.model_copy(update=updates),
    }
    return plan.model_copy(
        update=plan_updates
    )


def _should_inherit_session_contract(question: str, state: SessionState) -> bool:
    normalized = " ".join(question.split()).casefold()
    if not normalized:
        return False
    if _is_prior_result_reference(question):
        return True
    if _axis_followup_label(question) is not None:
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
    if _axis_followup_label(question) is not None:
        values.extend(state.referenced_entity_set or state.canonical_entities)
    else:
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
    interpreted = " ".join(
        (
            plan.resolved_question,
            *plan.requested_answer_shape.entities,
        )
    )
    if len(_kcd_codes(interpreted)) > 1:
        return None
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
            "status": (
                result.status
                if result.status != "ok"
                else ("ok" if usable else "empty")
            ),
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
    subject = _human_readable_web_subject(str(request["query"]))
    query = f"{subject} {periods} 공식 통계 발표 보도자료"
    queries = plan.tool_queries.model_copy(update={"web": (query,)})
    return plan.model_copy(
        update={
            "tool_queries": queries,
            "answer_sources": ("web",),
            "needs_second_hop": False,
            "linking_plan": "typed period gap fill via one web query",
        }
    )


def _human_readable_web_subject(value: str) -> str:
    entity = resolve_disease_entity(value)
    if entity is None:
        return " ".join(value.split())
    subject = value
    for alias in entity.aliases:
        if not any(character.isdigit() for character in alias):
            continue
        subject = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
            " ",
            subject,
            flags=re.IGNORECASE,
        )
    subject = " ".join(subject.split())
    return subject or entity.canonical_name


def _tag_gap_result(result: SourceResult, request: Mapping[str, Any]) -> SourceResult:
    payload = result.payload if isinstance(result.payload, Mapping) else {"value": result.payload}
    filtered_payload, usable = _official_gap_payload(payload)
    return result.model_copy(
        update={
            "status": (
                result.status
                if result.status != "ok"
                else ("ok" if usable else "empty")
            ),
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


def _section_paragraph_evidence_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    identifiers: dict[str, None] = {}
    for paragraphs in value.values():
        if not isinstance(paragraphs, Sequence):
            continue
        for paragraph in paragraphs:
            if not isinstance(paragraph, Mapping):
                continue
            raw_evidence_ids = paragraph.get("evidence_ids", ())
            if isinstance(raw_evidence_ids, Sequence) and not isinstance(
                raw_evidence_ids,
                (str, bytes),
            ):
                for item in raw_evidence_ids:
                    evidence_id = str(item or "").strip()
                    if evidence_id:
                        identifiers.setdefault(evidence_id, None)
            sources = [paragraph.get("evidence", ())]
            group = paragraph.get("evidence_group")
            if isinstance(group, Mapping):
                sources.append(group.get("members", ()))
            for source in sources:
                if not isinstance(source, Sequence):
                    continue
                for item in source:
                    if not isinstance(item, Mapping):
                        continue
                    evidence_id = str(item.get("evidence_id") or "").strip()
                    if evidence_id:
                        identifiers.setdefault(evidence_id, None)
    return tuple(identifiers)


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
    if host.endswith((".ac.kr", ".or.kr")) or host in {
        "yna.co.kr",
        "www.yna.co.kr",
    }:
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
