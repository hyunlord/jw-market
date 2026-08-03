from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from functools import lru_cache, wraps
from hmac import compare_digest
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from jw_chat_agent_poc.agent_loop import is_explicit_quarter_sales_question, should_use_agent_loop
from jw_chat_agent_poc.agent_loop.bq_planner import multi_brand_cardinality_message
from jw_chat_agent_poc.agent_loop.element_ledger import market_scope_defers_to_contract
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.factory import (
    ambiguous_brand_result,
    brand_unresolved_result,
    build_chat_agent_dependencies,
    build_tool_use_agent,
    unsupported_brand_result,
    unsupported_hira_interface_result,
)
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.planner import BrandUnresolvedError
from jw_chat_agent_poc.agent_loop.structured_planner import (
    preflight_structured_market_question,
    structured_metric_owner,
)
from jw_chat_agent_poc.common.periods import (
    has_explicit_period_cue,
    requested_period,
)
from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.orchestrator import ChatAgent
from jw_chat_agent_poc.contracts.routing import (
    RejectedRoute,
    RouteMode,
    unified_router_shadow_enabled,
)
from jw_chat_agent_poc.orchestrator.answer_contract import (
    enforce_answer_contract,
    evaluate_answer_contract,
    positioning_markdown_response,
)
from jw_chat_agent_poc.orchestrator.bq_mixed_analysis import build_file_market_analysis_call
from jw_chat_agent_poc.orchestrator.bq_runtime_guard import (
    BQAnalysisValidationError,
    validate_bq_analysis_call,
)
from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.deep_research import (
    DeepResearchToolPlanner,
    parse_deep_research_request,
)
from jw_chat_agent_poc.orchestrator.final_surface_assembly import apply_final_surface_assembly
from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.orchestrator.market_answer_contract import (
    enforce_market_answer_contract,
    is_actionable_upstream_guidance,
    render_same_market_sales_answer,
)
from jw_chat_agent_poc.orchestrator.operation_contract import (
    clear_current_query_spec,
    current_query_spec,
    observe_actual_coverage,
    observe_surface_coverage,
    set_current_query_spec,
)
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    ShadowGate,
    current_shadow_request_id,
    emit_shadow_gate_exception,
    question_fingerprint,
    shadow_request_id_scope,
    shadow_request_scope,
)
from jw_chat_agent_poc.orchestrator.typed_failure import (
    TypedFailureCode,
    normalize_typed_failure,
    observe_typed_failure,
)
from jw_chat_agent_poc.orchestrator.hira_disease import (
    explicit_hira_disease_code,
    hira_binding_question,
    is_hira_disease_question,
)
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_labels
from jw_chat_agent_poc.contracts.shadow import (
    evidence_bundle_shadow_observation,
    resolved_query_shadow_observation,
)
from jw_chat_agent_poc.orchestrator.query_spec import (
    RequestQuerySpec,
    extract_query_spec,
    query_spec_observation,
)
from jw_chat_agent_poc.orchestrator.response_format_contract import apply_response_format_contract
from jw_chat_agent_poc.orchestrator.route_decision_shadow import observe_route_decision
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.orchestrator.source_trap import requested_unavailable_source
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_use_requirements
from jw_chat_agent_poc.resolver import AmbiguousBrandError, UnsupportedBrandError
from jw_chat_agent_poc.service.answer_safety import (
    append_deterministic_source_block,
    cleanup_markdown_answer,
    ensure_natural_fact_lead,
    ensure_top_brand_trend_table,
    enforce_relational_numeric_claims_with_trace,
    finalized_fallback_fact_answer,
    replace_internal_fact_dump,
)
from jw_chat_agent_poc.service.answer_delivery import (
    record_answer_delivery,
    record_source_notice_attachment,
)
from jw_chat_agent_poc.service.answer_pipeline import (
    AnswerPipelineContext,
    build_answer_pipeline_stages,
    run_selected_answer_pipeline,
)
from jw_chat_agent_poc.service.markdown_cleanup import scrub_internal_terminology
from jw_chat_agent_poc.service.charts import build_charts, filter_charts_for_binding
from jw_chat_agent_poc.service.concurrency import BUSY_MESSAGE, ChatBusyError, ChatConcurrencyLimiter
from jw_chat_agent_poc.service.process_observability import process_observability
from jw_chat_agent_poc.service.conversation import (
    ConversationSlots,
    ConversationStore,
    ConversationTurn,
    DiseaseCodeCandidateSlot,
    PendingClarification,
)
from jw_chat_agent_poc.service.conversation_repository import (
    CONVERSATION_REPOSITORY_ENV,
    ConversationRepository,
    build_conversation_repository,
    conversation_repository_enabled,
)
from jw_chat_agent_poc.service.conversation_context import (
    anaphora_observation,
    extract_conversation_slots,
    requires_previous_turn,
    resolve_anaphora,
    reused_context_result,
    unresolved_reference_result,
)
from jw_chat_agent_poc.service.conversation_history import ConversationHistoryStore, MySQLConversationHistoryStore
from jw_chat_agent_poc.service.evidence_binding import (
    evidence_facts_from_result,
    expected_entities_from_result,
    expected_market_ids_from_result,
    verify_claim_bindings,
)
from jw_chat_agent_poc.service.evidence_binding_observability import (
    binding_context_observability,
    binding_pipeline_observability,
    evidence_fact_input_inventory,
)
from jw_chat_agent_poc.service.context_scope import (
    ContextScope,
    file_reference_terms,
    has_file_reference,
    matches_file_schema,
    resolve_context_scope,
)
from jw_chat_agent_poc.service.routing_boundary_contract import (
    AppScopeDecision,
    MarketRouteKind,
    MarketShortcutDecision,
    routing_boundaries_enabled,
)
from jw_chat_agent_poc.service.routing_boundaries_legacy import (
    legacy_app_scope_decision as _legacy_app_scope_decision,
    legacy_market_shortcut_decision as _legacy_market_shortcut_decision,
)
from jw_chat_agent_poc.service.file_search_client import (
    UploadedFileOverview,
    fetch_uploaded_file_overviews,
    fetch_uploaded_file_schema_columns,
    has_active_uploaded_file,
    search_uploaded_files,
)
from jw_chat_agent_poc.service.file_brief import render_uploaded_file_machine_brief
from jw_chat_agent_poc.service.file_llm_brief import (
    FileBriefValidationError,
    build_file_brief_messages,
    deserialize_file_overviews,
    parse_and_render_file_briefs,
    render_file_brief_grounding_text,
    serialize_file_overviews,
)
from jw_chat_agent_poc.service.file_sql_query import is_ambiguous_file_analysis_question
from jw_chat_agent_poc.service.genos_client import (
    GenosClient,
    append_source_basis_notice,
)
from jw_chat_agent_poc.service.general_view_routing import GeneralRoute
from jw_chat_agent_poc.service.history_projection import (
    HistoryProjectionRuntime,
    ProjectionRequestContext,
    sanitize_http_headers,
    trusted_portal_user_id,
)
from jw_chat_agent_poc.service.models import (
    COMBINED_FILE_CONTEXT_MAX_CHARS,
    QUESTION_MAX_CHARS,
    ChatAccepted,
    ChatAnswer,
    ChatRequest,
    HealthResponse,
)
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope, version_payload
from jw_chat_agent_poc.service.security_policy import (
    SEC12_BLOCKED_ANSWER,
    enforced_answer,
    evaluate_input_policy,
    evaluate_output_leakage,
    policy_is_enforced,
)
from jw_chat_agent_poc.service.sse_presenter import (
    SSE_PRESENTER_ENV,
    selected_sse_presenter,
)
from jw_chat_agent_poc.service.startup_warmup import (
    DisabledStartupWarmup,
    StartupWarmup,
    startup_warmup_from_env,
)
from jw_chat_agent_poc.common.timing import (
    StageEventSink,
    emit_completed_stage,
    ensure_timing,
    finish,
    public_stage_summary,
    request_span_scope,
    stage,
    stage_event_sink,
    suspend_request_spans,
    trace_span,
)
from jw_chat_agent_poc.common.token_usage import record_token_usage
from jw_chat_agent_poc.tool_use.integration import attach_routing_v4_legacy_observation
from jw_chat_agent_poc.tool_use.routing_v4_rules import (
    classify_question,
    classify_question_without_observation,
)
from jw_chat_agent_poc.tool_use.routing_v4_runtime import configured_routing_mode
from jw_chat_agent_poc.tool_use.routing_v4_types import RoutingMode
from jw_chat_agent_poc.tools.external import resolve_patent_ingredient_query
from jw_chat_agent_poc.tools.metrics.market_scope import (
    MarketScopeResolver,
    asks_market_members,
    detect_market_scope_intent,
    map_market_view_reply,
)


AgentFactory = Callable[..., ChatAgent]
LOGGER = logging.getLogger(__name__)
QUEUE_PROGRESS_THRESHOLD_S = 2.0
QUEUE_PROGRESS_INTERVAL_S = 2.5

DIRECT_ROUTE_API_KEY_ENV = "DIRECT_ROUTE_API_KEY"
DIRECT_ROUTE_AUTH_HOSTS_ENV = "DIRECT_ROUTE_AUTH_HOSTS"
DEFAULT_DIRECT_ROUTE_AUTH_HOSTS = frozenset(
    {
        "admin.dev.ai.jwhealthcare.com",
        "jwai-dev.jwhealthcare.com",
    }
)

CHAT_ACCEPTED_DESCRIPTION = """
질문을 처리하고 서버 메모리 세션에 결과를 저장한 뒤, 후속 `/chat/stream`에서 재사용할 `session_id`를 반환합니다.

응답은 `session_id`, 유지된 `conversation_id`, 사용 가능한 `sources` 라벨을 포함합니다. 최종 답변 본문이 필요하면 `/chat/answer`를 사용합니다.
"""

CHAT_ANSWER_DESCRIPTION = """
질문을 즉시 처리해 완성된 답변 JSON을 반환합니다.

응답 최상위 필드는 `text`(마크다운 답변), `charts`(근거 기반 차트 스펙 배열), `trace`(라우팅·도구·타이밍 추적), `sources`(출처 라벨), `conversation_id`입니다.
"""

CHAT_STREAM_DESCRIPTION = """
질문을 처리한 뒤 Server-Sent Events(SSE)로 답변을 스트리밍합니다. `session_id`가 있으면 `/chat`으로 저장된 결과를 재생하고, 없으면 `question`을 즉시 처리합니다.

## SSE Event Contract

| Event | Payload | Timing | Count |
|---|---|---|---|
| conversation | plain string conversation_id | first, if conversation_id exists | 0-1 |
| step | JSON `{index, name, detail, status, raw_name, raw_detail, elapsed_ms?}` | while live question processing stages start/finish | 0-N |
| sources | comma-separated source labels | before content | 1 |
| file_sources | JSON array `[{file_name, i_page?, sheet_name?}]` | after sources, only when uploaded-file grounding was used | 0-1 |
| delta | markdown text chunk | prose segments | 0-N |
| markdown_block | JSON `{kind, markdown}` | table segments | 0-N |
| charts | JSON array | if charts are present | 0-1 |
| timing | JSON `{stages, token_usage, ...}` | after content | 1 |
| trace | JSON full trace envelope | after timing | 1 |
| done | plain string `ok` | last | 1 |

`session_id` replay streams already-computed results and therefore does not emit new `step` progress events. Live `question` streams emit `step` events as processing stages start and finish, then reuse the same final answer event sequence.
"""


@dataclass(frozen=True, slots=True)
class FinalAnswer:
    text: str
    charts: list[dict[str, Any]]
    timing: dict[str, Any]
    trace: dict[str, Any]
    sources: tuple[str, ...]
    conversation_id: str | None
    file_sources: tuple[dict[str, Any], ...] = ()
    conversation_slots: ConversationSlots = ConversationSlots()


SESSION_STORE_MAX_ENV = "SESSION_STORE_MAX"
DEFAULT_SESSION_STORE_MAX = 500
CHART_AFTER_EVIDENCE_BINDING_ENV = "JW_CHAT_CHART_AFTER_EVIDENCE_BINDING"
HIRA_REIMBURSEMENT_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_HIRA_REIMBURSEMENT"
HIRA_DISEASE_STATS_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_HIRA_DISEASE_STATS"
MFDS_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_MFDS"
CLINICAL_TRIALS_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_CLINICAL_TRIALS"
CLINICAL_FB02_CUTOVER_ENV = "JW_CHAT_ROUTER_CUTOVER_CLINICAL_FB02"


def decide_app_scope_route(**kwargs: Any) -> AppScopeDecision:
    from jw_chat_agent_poc.service.routing_boundaries import decide_app_scope_route as implementation

    return implementation(**kwargs)


def decide_market_shortcut(**kwargs: Any) -> MarketShortcutDecision:
    from jw_chat_agent_poc.service.routing_boundaries import decide_market_shortcut as implementation

    return implementation(**kwargs)


def observe_unified_app_scope_shadow(**kwargs: Any) -> None:
    if not unified_router_shadow_enabled():
        return
    from jw_chat_agent_poc.orchestrator.unified_router_shadow import observe_app_scope_route

    observe_app_scope_route(**kwargs)


def observe_unified_market_shortcut_shadow(**kwargs: Any) -> None:
    if not unified_router_shadow_enabled():
        return
    from jw_chat_agent_poc.orchestrator.unified_router_shadow import observe_market_shortcut_route

    observe_market_shortcut_route(**kwargs)


def _hira_reimbursement_cutover_decision(**kwargs: Any) -> Any | None:
    if os.getenv(HIRA_REIMBURSEMENT_CUTOVER_ENV, "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return None
    try:
        from jw_chat_agent_poc.service.unified_router_cutover import (
            select_hira_reimbursement_cutover,
        )

        return select_hira_reimbursement_cutover(**kwargs)
    except Exception:  # noqa: BLE001 - cutover selection falls back to the proven legacy route
        LOGGER.exception("hira_reimbursement_cutover_selection_failed")
        return None


def _answer_hira_reimbursement_cutover(question: str, external_mode: str) -> dict | None:
    try:
        dependencies = build_chat_agent_dependencies(external_mode=external_mode)
        from jw_chat_agent_poc.tool_use.integration import run_enforced_external_tool_agent

        return run_enforced_external_tool_agent(
            question,
            resolver=dependencies.resolver,
            external=dependencies.external,
        )
    except Exception:  # noqa: BLE001 - unexpected setup failures retain the legacy execution path
        LOGGER.exception("hira_reimbursement_cutover_execution_failed")
        return None


def _hira_disease_stats_cutover_decision(**kwargs: Any) -> Any | None:
    if os.getenv(HIRA_DISEASE_STATS_CUTOVER_ENV, "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return None
    try:
        from jw_chat_agent_poc.service.unified_router_cutover import (
            select_hira_disease_stats_cutover,
        )

        return select_hira_disease_stats_cutover(**kwargs)
    except Exception:  # noqa: BLE001 - selection failures retain the proven legacy route
        LOGGER.exception("hira_disease_stats_cutover_selection_failed")
        return None


def _answer_hira_disease_stats_cutover(
    agent_factory,
    question: str,
    external_mode: str,
    agent_kwargs: dict[str, Any],
) -> dict | None:
    try:
        agent = agent_factory(external_mode=external_mode)
        return agent.answer(question, None, **agent_kwargs)
    except Exception:  # noqa: BLE001 - unexpected setup failures retain the legacy execution path
        LOGGER.exception("hira_disease_stats_cutover_execution_failed")
        return None


def _mfds_cutover_decision(**kwargs: Any) -> Any | None:
    if os.getenv(MFDS_CUTOVER_ENV, "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return None
    try:
        from jw_chat_agent_poc.service.unified_router_cutover import select_mfds_cutover

        return select_mfds_cutover(**kwargs)
    except Exception:  # noqa: BLE001 - selection failures retain the proven legacy route
        LOGGER.exception("mfds_cutover_selection_failed")
        return None


def _answer_mfds_cutover(question: str, external_mode: str) -> dict | None:
    try:
        dependencies = build_chat_agent_dependencies(external_mode=external_mode)
        from jw_chat_agent_poc.tool_use.integration import run_enforced_external_tool_agent

        result = run_enforced_external_tool_agent(
            question,
            resolver=dependencies.resolver,
            external=dependencies.external,
        )
        for call in result.get("tool_calls", []):
            render_data = call.get("render_data")
            if (
                call.get("tool") == "mfds_permission_search"
                and call.get("status") == "error"
                and isinstance(render_data, dict)
                and render_data.get("error_message")
            ):
                result["answer"] = str(render_data["error_message"])
                break
        return result
    except Exception:  # noqa: BLE001 - unexpected setup failures retain the legacy execution path
        LOGGER.exception("mfds_cutover_execution_failed")
        return None


def _clinical_trials_cutover_decision(**kwargs: Any) -> Any | None:
    if os.getenv(CLINICAL_TRIALS_CUTOVER_ENV, "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return None
    try:
        question = str(kwargs.get("question") or "")
        classification = classify_question_without_observation(question)
        requested_facets = set(classification.requested_facets)
        is_fb02_shape = {"clinical", "permission"}.issubset(requested_facets)
        if is_fb02_shape and os.getenv(CLINICAL_FB02_CUTOVER_ENV, "1").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return None
        from jw_chat_agent_poc.service.unified_router_cutover import (
            select_clinical_trials_cutover,
        )

        return select_clinical_trials_cutover(**kwargs)
    except Exception:  # noqa: BLE001 - selection failures retain the proven legacy route
        LOGGER.exception("clinical_trials_cutover_selection_failed")
        return None


def _answer_clinical_trials_cutover(
    agent_factory,
    question: str,
    external_mode: str,
    agent_kwargs: dict[str, Any],
) -> dict | None:
    try:
        agent = agent_factory(external_mode=external_mode)
        return agent.answer(question, None, **agent_kwargs)
    except Exception:  # noqa: BLE001 - unexpected setup failures retain the legacy execution path
        LOGGER.exception("clinical_trials_cutover_execution_failed")
        return None


def _chart_after_evidence_binding_enabled() -> bool:
    return os.environ.get(CHART_AFTER_EVIDENCE_BINDING_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


class SessionStore:
    def __init__(self, conversations: ConversationStore | None = None, max_sessions: int | None = None) -> None:
        if max_sessions is None:
            max_sessions = int(os.environ.get(SESSION_STORE_MAX_ENV, str(DEFAULT_SESSION_STORE_MAX)))
        self._max_sessions = max(1, max_sessions)
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._conversation_cache = conversations or ConversationStore(max_states=self._max_sessions)
        self._conversation_history: ConversationHistoryStore | None = None
        self._conversation_repository_enabled = conversation_repository_enabled()
        self.conversations: ConversationRepository = build_conversation_repository(
            self._conversation_cache,
            None,
        )

    def configure_conversation_repository(
        self,
        history: ConversationHistoryStore | None,
    ) -> None:
        enabled = conversation_repository_enabled()
        with self._lock:
            if (
                history is self._conversation_history
                and enabled == self._conversation_repository_enabled
            ):
                return
            self.conversations = build_conversation_repository(self._conversation_cache, history)
            self._conversation_history = history
            self._conversation_repository_enabled = enabled

    def put(self, item: dict) -> str:
        session_id = uuid4().hex
        with self._lock:
            self._items[session_id] = item
            while len(self._items) > self._max_sessions:
                self._items.popitem(last=False)
        return session_id

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            item = self._items.get(session_id)
            if item is not None:
                self._items.move_to_end(session_id)
            return item


class _AnswerItem(dict):
    operation_contract_query_spec: RequestQuerySpec | None = None
    shadow_request_id: str = ""


def _configured_direct_route_hosts() -> set[str]:
    raw_value = os.environ.get(DIRECT_ROUTE_AUTH_HOSTS_ENV)
    if raw_value is None:
        return set(DEFAULT_DIRECT_ROUTE_AUTH_HOSTS)
    return {host.strip().lower() for host in raw_value.split(",") if host.strip()}


def _header_host(value: str | None) -> str:
    if not value:
        return ""
    return value.split(",", 1)[0].strip().split(":", 1)[0].lower()


def _is_direct_public_request(request: Request) -> bool:
    hosts = _configured_direct_route_hosts()
    if not hosts:
        return False
    if "*" in hosts:
        return True
    request_host = _header_host(request.headers.get("host"))
    forwarded_host = _header_host(request.headers.get("x-forwarded-host"))
    return request_host in hosts or forwarded_host in hosts


def _require_direct_route_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_portal_user_id: str | None = Header(default=None, alias="X-Portal-User-Id"),
) -> ProjectionRequestContext:
    public_request = _is_direct_public_request(request)
    if not public_request:
        return ProjectionRequestContext(portal_user_id=None, http_headers=sanitize_http_headers(request.headers))
    expected_key = os.environ.get(DIRECT_ROUTE_API_KEY_ENV)
    if not expected_key:
        raise HTTPException(status_code=503, detail="direct route API key is not configured")
    if x_api_key is None or not compare_digest(x_api_key, expected_key):
        raise HTTPException(status_code=401, detail="invalid API key")
    try:
        portal_user_id = trusted_portal_user_id(
            x_portal_user_id,
            public_request=True,
            api_key_authenticated=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectionRequestContext(
        portal_user_id=portal_user_id,
        http_headers=sanitize_http_headers(request.headers),
    )


class InputSizeLimitError(ValueError):
    def __init__(self, *, field: str, max_chars: int) -> None:
        super().__init__(f"{field} exceeds {max_chars} characters")
        self.field = field
        self.max_chars = max_chars


def create_app(
    *,
    agent_factory: AgentFactory | None = None,
    market_scope_resolver: MarketScopeResolver | None = None,
    store: SessionStore | None = None,
    history_store: ConversationHistoryStore | None = None,
    projection_runtime: HistoryProjectionRuntime | None = None,
    concurrency_limiter: ChatConcurrencyLimiter | None = None,
    startup_warmup: StartupWarmup | None = None,
) -> FastAPI:
    app = FastAPI(title="JW Chat Agent POC", version="0.2.0", root_path="/jw-chat-agent")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    store = store or SessionStore()
    limiter = concurrency_limiter or ChatConcurrencyLimiter()
    resolver = market_scope_resolver or MarketScopeResolver()
    make_agent = agent_factory or _default_agent_factory
    use_direct_agent_loop = agent_factory is None
    projection = projection_runtime or HistoryProjectionRuntime.from_env()
    history = history_store or MySQLConversationHistoryStore(projection_outbox=projection.outbox)
    store.configure_conversation_repository(history)
    warmup = startup_warmup or DisabledStartupWarmup()

    @app.exception_handler(RequestValidationError)
    async def compact_input_size_validation_error(
        request: Request,
        exc: RequestValidationError,
    ):
        if any(error.get("type") == "string_too_long" for error in exc.errors()):
            return JSONResponse(
                status_code=413,
                content={
                    "detail": {
                        "code": "input_too_large",
                        "message": "입력 길이가 허용 범위를 초과했습니다.",
                    }
                },
            )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(InputSizeLimitError)
    async def combined_input_size_error(
        _request: Request,
        exc: InputSizeLimitError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "code": "input_too_large",
                    "field": exc.field,
                    "max_chars": exc.max_chars,
                }
            },
        )

    @app.on_event("startup")
    def start_history_projection_worker() -> None:
        projection.start()
        warmup.start()

    @app.on_event("shutdown")
    def stop_history_projection_worker() -> None:
        projection.stop()

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/readyz", response_model=HealthResponse)
    def readyz() -> HealthResponse:
        if not warmup.is_ready():
            raise HTTPException(status_code=503, detail="strategic mart startup warmup is in progress")
        return HealthResponse(status="ok")

    @app.get("/__version")
    def version() -> dict:
        return version_payload()

    @app.get("/__runtime/observability")
    def runtime_observability() -> dict[str, dict[str, Any]]:
        return {
            "conversation": store.conversations.observability(),
            **resolver.runtime_observability(),
            "process": process_observability(),
        }

    @app.post(
        "/chat",
        response_model=ChatAccepted,
        summary="Create a chat session result",
        description=CHAT_ACCEPTED_DESCRIPTION,
    )
    def chat(request: ChatRequest, _api_key: None = Depends(_require_direct_route_api_key)) -> ChatAccepted:
        documents = tuple(Path(path) for path in request.document_paths)
        if not request.question.strip() and not _has_file_signal(list(documents), request.file_context):
            raise HTTPException(status_code=400, detail="질문 또는 파일 업로드가 필요합니다.")
        try:
            with limiter.slot():
                result = _answer_question(
                    store,
                    resolver,
                    make_agent,
                    request.question,
                    request.external_mode,
                    request.conversation_id,
                    list(documents),
                    request.file_context,
                    use_direct_agent_loop=use_direct_agent_loop,
                    conversation_history=history,
                )
        except ChatBusyError as exc:
            raise HTTPException(status_code=503, detail=BUSY_MESSAGE) from exc
        stored_item = _AnswerItem(
            {"question": request.question, "result": result["result"]}
        )
        stored_item.operation_contract_query_spec = (
            getattr(result, "operation_contract_query_spec", None)
        )
        stored_item.shadow_request_id = getattr(result, "shadow_request_id", "")
        session_id = store.put(stored_item)
        return ChatAccepted(
            session_id=session_id,
            conversation_id=result["conversation_id"],
            sources=tuple(result["result"].get("sources", ())),
        )

    @app.post(
        "/chat/answer",
        response_model=ChatAnswer,
        summary="Return a completed chat answer",
        description=CHAT_ANSWER_DESCRIPTION,
    )
    def chat_answer(
        request: ChatRequest,
        projection_context: ProjectionRequestContext = Depends(_require_direct_route_api_key),
    ) -> ChatAnswer:
        documents = tuple(Path(path) for path in request.document_paths)
        if not request.question.strip() and not _has_file_signal(list(documents), request.file_context):
            raise HTTPException(status_code=400, detail="질문 또는 파일 업로드가 필요합니다.")
        try:
            with limiter.slot():
                item = _answer_question(
                    store,
                    resolver,
                    make_agent,
                    request.question,
                    request.external_mode,
                    request.conversation_id,
                    list(documents),
                    request.file_context,
                    use_direct_agent_loop=use_direct_agent_loop,
                    conversation_history=history,
                )
                with shadow_request_id_scope(item.shadow_request_id):
                    final_answer = _compute_final_answer_with_query_spec(
                        item["question"],
                        item["result"],
                        item.get("conversation_id"),
                        item.operation_contract_query_spec,
                    )
        except ChatBusyError as exc:
            raise HTTPException(status_code=503, detail=BUSY_MESSAGE) from exc
        _record_conversation_history(
            history,
            session_id=None,
            question=item["question"],
            final_answer=final_answer,
            projection_context=projection_context,
        )
        return ChatAnswer(
            text=final_answer.text,
            charts=final_answer.charts,
            trace=final_answer.trace,
            sources=final_answer.sources,
            conversation_id=final_answer.conversation_id,
            file_sources=list(_project_public_file_sources(final_answer.file_sources)),
        )

    @app.get(
        "/chat/stream",
        summary="Stream a chat answer as Server-Sent Events",
        description=CHAT_STREAM_DESCRIPTION,
    )
    def chat_stream(
        session_id: str | None = Query(default=None),
        question: str | None = Query(default=None, max_length=QUESTION_MAX_CHARS),
        external_mode: str = Query(default="live"),
        conversation_id: str | None = Query(default=None),
        projection_context: ProjectionRequestContext = Depends(_require_direct_route_api_key),
    ) -> StreamingResponse:
        if session_id:
            item = _resolve_session(
                store,
                resolver,
                make_agent,
                session_id,
                None,
                external_mode,
                conversation_id,
                use_direct_agent_loop=use_direct_agent_loop,
            )
            return StreamingResponse(
                _sse_events(
                    item["question"],
                    item["result"],
                    item.get("conversation_id"),
                    history_store=history,
                    session_id=session_id,
                    projection_context=projection_context,
                    limiter=limiter,
                    query_spec=getattr(
                        item,
                        "operation_contract_query_spec",
                        None,
                    ),
                    shadow_request_id=getattr(item, "shadow_request_id", ""),
                ),
                media_type="text/event-stream",
            )
        if not question:
            raise HTTPException(status_code=400, detail="session_id or question is required")
        return StreamingResponse(
            _stream_resolving_session_events(
                store,
                resolver,
                make_agent,
                question,
                external_mode,
                conversation_id,
                use_direct_agent_loop=use_direct_agent_loop,
                history_store=history,
                projection_context=projection_context,
                limiter=limiter,
            ),
            media_type="text/event-stream",
        )

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return _frontend_file_response("index.html")

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def frontend_file(frontend_path: str) -> FileResponse:
        normalized_path = _normalize_frontend_path(frontend_path)
        return _frontend_file_response(normalized_path)

    return app


def _resolve_session(
    store: SessionStore,
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    session_id: str | None,
    question: str | None,
    external_mode: str,
    conversation_id: str | None = None,
    *,
    use_direct_agent_loop: bool = False,
) -> dict:
    if session_id:
        item = store.get(session_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        return item
    if question:
        return _answer_question(
            store,
            market_scope_resolver,
            agent_factory,
            question,
            external_mode,
            conversation_id,
            use_direct_agent_loop=use_direct_agent_loop,
        )
    raise HTTPException(status_code=400, detail="session_id or question is required")


def _capture_request_spans(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with request_span_scope() as spans:
            item = function(*args, **kwargs)
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, dict):
            result["_qa_spans"] = list(spans)
        return item

    return wrapped


def _capture_operation_contract_query_spec(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        clear_current_query_spec()
        try:
            item = function(*args, **kwargs)
            answer_item = _AnswerItem(item)
            answer_item.operation_contract_query_spec = current_query_spec()
            answer_item.shadow_request_id = current_shadow_request_id()
            return answer_item
        finally:
            clear_current_query_spec()

    return wrapped


def _observe_query_spec(
    question: str,
    market_scope_resolver: MarketScopeResolver,
) -> None:
    clear_current_query_spec()
    try:
        with suspend_request_spans():
            query_spec = extract_query_spec(
                question,
                market_scope_resolver,
                build_period_grounding(question),
            )
    except Exception:  # noqa: BLE001 - stage-0 observation cannot alter request behavior
        LOGGER.exception("request_query_spec_observation_failed")
        return
    set_current_query_spec(
        query_spec,
        question_fingerprint=question_fingerprint(question),
    )
    LOGGER.info(
        "request_query_spec_observed spec=%s",
        query_spec_observation(query_spec),
    )
    try:
        observation = resolved_query_shadow_observation(query_spec)
    except Exception:  # noqa: BLE001 - shadow contract creation must remain fail-open
        LOGGER.exception("resolved_query_shadow_observation_failed")
        return
    LOGGER.info("resolved_query_shadow_observed observation=%s", observation)


@shadow_request_scope
@_capture_request_spans
@_capture_operation_contract_query_spec
def _answer_question(
    store: SessionStore,
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    question: str,
    external_mode: str,
    conversation_id: str | None,
    documents: list[Path] | None = None,
    file_context: str | None = None,
    *,
    use_direct_agent_loop: bool = False,
    timing_sink: StageEventSink | None = None,
    conversation_history: ConversationHistoryStore | None = None,
) -> dict:
    store.configure_conversation_repository(conversation_history)
    input_policy_decision = evaluate_input_policy(question)
    if policy_is_enforced(input_policy_decision):
        state = store.conversations.get_or_create(conversation_id)
        result = {
            "answer": SEC12_BLOCKED_ANSWER,
            "conversation_fallback_ready": True,
            "sources": [],
            "tool_calls": [],
            "_sec12_input_policy_decision": input_policy_decision,
        }
        store.conversations.record_exchange(
            state.conversation_id,
            question,
            SEC12_BLOCKED_ANSWER,
            {},
            slots=extract_conversation_slots(result),
        )
        return {"question": question, "result": result, "conversation_id": state.conversation_id}
    _observe_query_spec(question, market_scope_resolver)
    sink_context = stage_event_sink(timing_sink) if timing_sink is not None else nullcontext()
    with sink_context:
        deep_request = parse_deep_research_request(question)
        effective_question = deep_request.question
        known_brand = getattr(market_scope_resolver, "has_explicit_brand_anchor", None)
        with trace_span("conversation_state_load", "in-memory conversation state lookup"):
            state = store.conversations.get_or_create(conversation_id)
        if conversation_id and not state.turns and requires_previous_turn(
            effective_question,
            known_brand=known_brand,
        ):
            _hydrate_latest_conversation_turn(store, state.conversation_id)
            with trace_span("conversation_state_reload", "state lookup after persisted history hydration"):
                state = store.conversations.get_or_create(state.conversation_id)
        provided_file = _has_file_signal(documents, file_context)
        file_probe_started = time.perf_counter()
        has_file = provided_file or bool(conversation_id and has_active_uploaded_file(state.conversation_id))
        file_probe_elapsed_ms = (time.perf_counter() - file_probe_started) * 1000
        schema_probe_started = time.perf_counter()
        file_schema_columns = (
            fetch_uploaded_file_schema_columns(state.conversation_id)
            if has_file and not provided_file
            else ()
        )
        schema_probe_elapsed_ms = (time.perf_counter() - schema_probe_started) * 1000
        file_overviews = (
            fetch_uploaded_file_overviews(state.conversation_id)
            if has_file and not effective_question.strip()
            else ()
        )
        previous_turn = state.turns[-1] if state.turns else None
        with trace_span("anaphora_resolution", "deterministic previous-turn slot resolution"):
            routing_resolution = resolve_anaphora(
                effective_question,
                previous_turn,
                known_brand=known_brand,
            )
        routing_question = routing_resolution.resolved_question
        has_explicit_market_anchor = market_scope_resolver.has_explicit_anchor(routing_question)
        metric_owner = structured_metric_owner(routing_question)
        needs_brand_clarification = (
            metric_owner == "brand"
            and not has_explicit_market_anchor
            and not has_file_reference(routing_question)
            and _is_entity_free_brand_metric_question(routing_question)
        )
        grounded_market_question = _ground_unanchored_market_golden(
            routing_question,
            has_explicit_anchor=False,
        )
        uses_monthly_market_golden = _uses_monthly_market_golden(
            routing_question,
            grounded_market_question,
        )
        # Every golden rewrite above binds the question to a synthetic anchor brand, not
        # just the two monthly contracts. Captured before routing_question is reassigned
        # below, because after that the two names are equal and the rewrite is invisible.
        uses_synthetic_market_anchor = grounded_market_question != routing_question
        execution_question = (
            grounded_market_question
            if grounded_market_question != routing_question
            else effective_question
        )
        routing_question = grounded_market_question
        needs_market_clarification = (
            structured_metric_owner(routing_question) == "market"
            and not has_file_reference(routing_question)
            and _is_entity_free_market_metric_question(routing_question)
        )
        has_market_intent = deep_request.enabled or _has_market_intent(
            routing_question,
            has_brand_anchor=has_explicit_market_anchor,
        )
        has_market_anchor = (
            market_scope_resolver.has_explicit_anchor(routing_question) if has_market_intent else False
        )
        inherit_file_context = has_file and not (
            has_market_intent
            and has_market_anchor
            and not has_file_reference(routing_question)
        )
        file_question = (
            _resolve_file_question(routing_question, previous_turn)
            if inherit_file_context
            else routing_question
        )
        app_scope_kwargs = {
            "file_question": file_question,
            "effective_question": effective_question,
            "has_file": has_file,
            "is_fresh_upload": bool(documents),
            "has_market_intent": has_market_intent,
            "has_market_anchor": has_market_anchor,
            "file_schema_columns": file_schema_columns,
            "needs_brand_clarification": needs_brand_clarification,
            "needs_market_clarification": needs_market_clarification,
            "resolve_context_scope_fn": resolve_context_scope,
            "matches_file_schema_fn": matches_file_schema,
            "has_file_reference_fn": has_file_reference,
        }
        with trace_span("context_scope_resolution", "market, file, and mixed scope classification"):
            app_scope_decision = (
                decide_app_scope_route(**app_scope_kwargs)
                if routing_boundaries_enabled()
                else _legacy_app_scope_decision(**app_scope_kwargs)
            )
        context_scope = app_scope_decision.context_scope
        if deep_request.enabled or context_scope in {ContextScope.FILE, ContextScope.MIXED}:
            emit_completed_stage(
                None,
                "file_session_probe",
                file_probe_elapsed_ms,
                "active uploaded file check",
            )
            emit_completed_stage(
                None,
                "file_schema_probe",
                schema_probe_elapsed_ms,
                "active uploaded file schema check",
            )
        needs_scope_clarification = app_scope_decision.needs_scope_clarification and not (
            _has_explicit_file_sheet_reference(effective_question)
        )
        observe_route_decision(
            question=effective_question,
            domain=context_scope.value,
            handler="context_scope_dispatch",
            mode=RouteMode.DETERMINISTIC,
            decided_by="app_scope",
            reason_codes=(
                f"scope:{context_scope.value}",
                f"file_present:{str(has_file).lower()}",
                f"market_intent:{str(has_market_intent).lower()}",
            ),
            rejected_alternatives=tuple(
                RejectedRoute(
                    domain=alternative.value,
                    handler="context_scope_dispatch",
                    reason_codes=("scope_not_selected",),
                )
                for alternative in ContextScope
                if alternative is not context_scope
            ),
            clarification_message=(
                "scope clarification required" if needs_scope_clarification else None
            ),
        )
        observe_unified_app_scope_shadow(
            question=effective_question,
            file_question=file_question,
            effective_question=effective_question,
            has_file=has_file,
            is_fresh_upload=bool(documents),
            has_market_intent=has_market_intent,
            has_market_anchor=has_market_anchor,
            file_schema_columns=tuple(file_schema_columns),
            needs_brand_clarification=needs_brand_clarification,
            needs_market_clarification=needs_market_clarification,
            legacy_domain=context_scope.value,
            legacy_handler="context_scope_dispatch",
            legacy_mode=RouteMode.DETERMINISTIC,
            deep_research=deep_request.enabled,
        )
        delegated_file_context: str | None = None
        file_source_items: tuple[dict[str, Any], ...] = ()
        deep_file_names: tuple[str, ...] = ()
        deterministic_file_answer = ""
        file_sql_trace: tuple[dict[str, str], ...] = ()
        if routing_resolution.unresolved_reference:
            result = unresolved_reference_result(effective_question)
        elif needs_brand_clarification:
            expires_at = store.conversations.pending_expiry()
            store.conversations.set_pending(
                state.conversation_id,
                PendingClarification(
                    kind="brand_metric",
                    original_question=effective_question,
                    brand="",
                    metric="sales",
                    created_at=expires_at - store.conversations.pending_ttl_seconds,
                    expires_at=expires_at,
                ),
            )
            result = _brand_metric_clarification_result(effective_question)
        elif needs_market_clarification:
            result = _market_metric_clarification_result(effective_question)
        elif uses_monthly_market_golden:
            result = market_scope_resolver.answer_monthly_market_golden(
                effective_question,
                anchor_brand="리바로",
            )
        elif deep_request.enabled:
            context_scope = ContextScope.MARKET
            if has_file:
                with stage(
                    None,
                    "deep_research_file_batch",
                    "현재 세션 업로드 파일 전체 근거 수집",
                ) as progress:
                    (
                        delegated_file_context,
                        file_source_items,
                        _has_active_upload,
                        _deep_file_sql_answer,
                        file_sql_trace,
                    ) = _delegated_file_context(
                        effective_question,
                        state.conversation_id,
                        file_context,
                        include_all_files=True,
                    )
                    deep_file_names = tuple(
                        dict.fromkeys(
                            str(item.get("file_name") or "").strip()
                            for item in file_source_items
                            if str(item.get("file_name") or "").strip()
                        )
                    )
                    progress.summary = f"{len(deep_file_names)}개 파일 근거 확인"
            result = (
                _answer_deep_research(routing_question, external_mode)
                if routing_question
                else _deep_research_clarification_result()
            )
            diagnostics = dict(result.get("router_diagnostics") or {})
            diagnostics.update(
                {
                    "mode": "deep_research",
                    "model": "gemini-3.1-pro-preview",
                    "serving_id": "202",
                    "trigger": "/deep",
                    "deep_file_source_count": len(deep_file_names) if has_file else 0,
                    "evidence_scope": (
                        "uploaded_files+market+external+web"
                        if delegated_file_context
                        else "market+external+web"
                    ),
                }
            )
            result = {
                **result,
                "research_mode": "deep",
                "effective_question": routing_question or effective_question,
                "requested_question": effective_question,
                "router_diagnostics": diagnostics,
            }
            if routing_resolution.interpretation_notice:
                result["conversation_interpretation"] = routing_resolution.interpretation_notice
        elif needs_scope_clarification:
            result = _scope_clarification_result(effective_question)
        elif context_scope is ContextScope.MIXED:
            result = _answer_mixed_parallel(
                store,
                market_scope_resolver,
                agent_factory,
                state.conversation_id,
                effective_question,
                external_mode,
                file_context,
                file_question=file_question,
                use_direct_agent_loop=use_direct_agent_loop,
            )
        elif context_scope is ContextScope.FILE:
            (
                delegated_file_context,
                file_source_items,
                _has_active_upload,
                deterministic_file_answer,
                file_sql_trace,
            ) = _delegated_file_context(file_question, state.conversation_id, file_context)
            if not effective_question.strip() and _has_file_signal(documents, delegated_file_context):
                result = _file_only_ready_result(
                    documents,
                    delegated_file_context,
                    file_overviews=file_overviews,
                )
            else:
                result = _file_scoped_result(effective_question)
        else:
            result = _answer_with_conversation(
                store,
                market_scope_resolver,
                agent_factory,
                state.conversation_id,
                execution_question,
                external_mode,
                [],
                use_direct_agent_loop=use_direct_agent_loop,
            )
        if not effective_question.strip() and context_scope is ContextScope.FILE and not result.get("file_only_ready"):
            result = _file_only_ready_result(
                documents,
                delegated_file_context,
                file_overviews=file_overviews,
            )
        if context_scope is ContextScope.MARKET and execution_question != effective_question:
            result = {**result, "effective_question": execution_question}
        result = _enforce_file_scope_isolation(result, effective_question, context_scope)
        if deterministic_file_answer and context_scope is ContextScope.FILE:
            result = {**result, "deterministic_file_answer": deterministic_file_answer}
        if file_sql_trace and (context_scope is ContextScope.FILE or deep_request.enabled):
            diagnostics = dict(result.get("router_diagnostics") or {})
            diagnostics["file_sql"] = [dict(item) for item in file_sql_trace]
            result = {**result, "router_diagnostics": diagnostics}
        result = _attach_file_context(result, delegated_file_context, file_source_items)
        result = _annotate_context_scope(result, context_scope)
        # Record what the resolver did with this question before the router saw
        # it. Attached on every branch, so a question that was handed to the
        # router unrewritten is as visible as one that was resolved. Kept out of
        # router_diagnostics, which carries the router's own decisions and is
        # compared whole by its callers.
        result = {
            **result,
            "_qa_anaphora": anaphora_observation(routing_resolution),
            "_sec12_input_policy_decision": input_policy_decision,
        }
        if uses_synthetic_market_anchor:
            # extract_conversation_slots reads this, so the stored turn records that its
            # anchor brand came from the rewrite rather than from the user.
            result = {**result, "anchor_brand_is_synthetic": True}
        with trace_span("conversation_state_persist", "persist resolved turn slots in request state"):
            store.conversations.record_exchange(
                state.conversation_id,
                question,
                str(result.get("answer") or ""),
                _applied_filters(result),
                slots=extract_conversation_slots(result),
            )
        return {"question": question, "result": result, "conversation_id": state.conversation_id}


def _hydrate_latest_conversation_turn(
    store: SessionStore,
    conversation_id: str,
) -> None:
    store.conversations.hydrate_latest(conversation_id)


def _answer_mixed_parallel(
    store: SessionStore,
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    conversation_id: str,
    question: str,
    external_mode: str,
    file_context: str | None,
    *,
    file_question: str | None = None,
    use_direct_agent_loop: bool,
) -> dict:
    started = time.perf_counter()
    timeout_s = max(1.0, float(os.getenv("JW_CHAT_MIXED_LEG_TIMEOUT_S", "90")))
    total_timeout_s = max(1.0, float(os.getenv("JW_CHAT_MIXED_TOTAL_TIMEOUT_S", "95")))
    deadline = started + total_timeout_s
    market_question = _mixed_market_question(question)
    resolved_file_question = file_question or question
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mixed-m1")
    file_future = executor.submit(
        _delegated_file_context,
        resolved_file_question,
        conversation_id,
        file_context,
    )
    market_future = executor.submit(
        _answer_with_conversation,
        store,
        market_scope_resolver,
        agent_factory,
        conversation_id,
        market_question,
        external_mode,
        [],
        use_direct_agent_loop=use_direct_agent_loop,
    )
    try:
        with stage(None, "mixed_file_leg", "uploaded file retrieval"):
            try:
                file_payload = file_future.result(timeout=min(timeout_s, _remaining_seconds(deadline)))
            except FutureTimeoutError:
                file_future.cancel()
                file_payload = (None, (), True, "", ())
            except Exception as exc:
                LOGGER.warning("mixed file leg failed type=%s", type(exc).__name__)
                file_payload = (None, (), True, "", ())
        with stage(None, "mixed_market_leg", "market fact retrieval"):
            try:
                market_result = market_future.result(timeout=min(timeout_s, _remaining_seconds(deadline)))
            except FutureTimeoutError:
                market_future.cancel()
                market_result = _mixed_market_failure_result(question)
            except Exception as exc:
                LOGGER.warning("mixed market leg failed type=%s", type(exc).__name__)
                market_result = _mixed_market_failure_result(question)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if len(file_payload) == 4:
        context, source_items, _has_active, deterministic_answer = file_payload
        file_sql_trace = ()
    else:
        context, source_items, _has_active, deterministic_answer, file_sql_trace = file_payload
    file_result = _file_scoped_result(resolved_file_question)
    file_result = _attach_file_context(file_result, context, source_items)
    if deterministic_answer:
        file_result["deterministic_file_answer"] = deterministic_answer
    if file_sql_trace:
        diagnostics = dict(file_result.get("router_diagnostics") or {})
        diagnostics["file_sql"] = [dict(item) for item in file_sql_trace]
        file_result["router_diagnostics"] = diagnostics
    if not context:
        file_result["mixed_leg_error"] = "첨부 문서 근거를 가져오지 못했습니다."

    combined = dict(market_result)
    combined["mixed_market_result"] = dict(market_result)
    combined["mixed_file_result"] = file_result
    combined["mixed_market_question"] = market_question
    combined["mixed_deadline_monotonic"] = deadline
    combined["sources"] = list(
        dict.fromkeys([*market_result.get("sources", []), *file_result.get("sources", [])])
    )
    market_calls = [
        call
        for call in market_result.get("tool_calls", [])
        if isinstance(call, dict)
    ]
    analysis_call = build_file_market_analysis_call(market_calls, deterministic_answer)
    analysis_status = "MISSING_EVIDENCE"
    if analysis_call is not None:
        try:
            validate_bq_analysis_call(analysis_call)
        except BQAnalysisValidationError as exc:
            LOGGER.warning("mixed BQ analysis rejected reason=%s", exc)
            analysis_status = "VERIFICATION_FAIL"
        else:
            combined["tool_calls"] = [*market_calls, analysis_call]
            analysis_status = "ok"
    diagnostics = dict(combined.get("router_diagnostics") or {})
    diagnostics.update(
        {
            "mode": "mixed_m1_parallel",
            "mixed_leg_timeout_s": timeout_s,
            "mixed_total_timeout_s": total_timeout_s,
            "mixed_synthesis_llm_calls": 0,
            "mixed_bq_analysis_validation": analysis_status,
        }
    )
    combined["router_diagnostics"] = diagnostics
    return combined


def _remaining_seconds(deadline: float) -> float:
    return max(0.001, deadline - time.perf_counter())


def _mixed_market_question(question: str) -> str:
    normalized = question.strip()
    lowered = normalized.lower()
    positions = [
        (lowered.find(term.lower()), term)
        for term in file_reference_terms()
        if lowered.find(term.lower()) >= 0
    ]
    if not positions:
        return normalized
    position, term = min(positions, key=lambda item: item[0])
    if position > 0:
        prefix = re.sub(r"(?:과|와|랑|이랑)\s*$", "", normalized[:position]).strip(" ,")
        return f"{prefix} 알려줘" if prefix else normalized
    remainder = normalized[position + len(term) :]
    match = re.search(r"(?:과|와|랑|이랑)\s+(.+)", remainder)
    if match:
        market_part = re.sub(r"(?:비교|대비).*$", "", match.group(1)).strip(" ,")
        if market_part:
            return f"{market_part} 알려줘"
    return normalized


def _mixed_market_failure_result(question: str) -> dict:
    answer = "시장 데이터 조회를 완료하지 못했습니다. 조회 오류입니다."
    return {
        "question": question,
        "answer": answer,
        "sources": [],
        "tool_calls": [],
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
        "mixed_leg_error": answer,
    }


def _scope_clarification_result(question: str) -> dict:
    answer = "파일과 시장 중 어느 근거를 사용할지 확인이 필요합니다. 브랜드·시장 또는 첨부 문서를 지정해 주세요."
    return {
        "question": question,
        "answer": answer,
        "sources": [],
        "tool_calls": [],
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
        "router_diagnostics": {"mode": "scope_clarification", "deterministic_execution": True},
    }


def _brand_metric_clarification_result(question: str) -> dict:
    period_match = re.search(r"(20\d{2})년?\s*([1-4])분기", question)
    month_match = re.search(r"(20\d{2})년?\s*(1[0-2]|0?[1-9])월", question)
    if period_match is not None:
        metric_label = f"{period_match.group(1)}년 {period_match.group(2)}분기 매출"
    elif month_match is not None:
        metric_label = f"{month_match.group(1)}년 {int(month_match.group(2))}월 매출"
    elif re.search(r"가장\s*최근\s*월\s*매출", question):
        metric_label = "가장 최근 월 매출"
    elif re.search(r"점유율", question):
        metric_label = "시장 점유율 변화" if re.search(r"변화|추이", question) else "시장 점유율"
    else:
        metric_label = "해당 기간 매출"
    answer = f"어느 브랜드의 {metric_label}인지 알려주세요."
    return {
        "question": question,
        "answer": answer,
        "sources": [],
        "tool_calls": [],
        "decomposition": [{"intent": "market_clarification", "status": "needs_clarification"}],
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
        "router_diagnostics": {"mode": "brand_clarification", "deterministic_execution": True},
    }


def _market_metric_clarification_result(question: str) -> dict:
    if re.search(r"상위\s*\d*|top\s*\d*", question, re.IGNORECASE):
        metric_label = "상위 브랜드"
    elif re.search(r"HHI|CR5|집중도", question, re.IGNORECASE):
        metric_label = "시장 집중도"
    else:
        metric_label = "시장 규모"
    answer = f"어느 시장의 {metric_label}인지 알려주세요."
    return {
        "question": question,
        "answer": answer,
        "sources": [],
        "tool_calls": [],
        "decomposition": [{"intent": "market_clarification", "status": "needs_clarification"}],
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
        "router_diagnostics": {"mode": "market_clarification", "deterministic_execution": True},
    }


def _delegated_file_context(
    question: str,
    conversation_id: str | None,
    file_context: str | None,
    *,
    include_all_files: bool = False,
) -> tuple[str | None, tuple[dict[str, Any], ...], bool, str, tuple[dict[str, str], ...]]:
    contexts: list[str] = []
    file_source_items: tuple[dict[str, Any], ...] = ()
    uploaded = (
        search_uploaded_files(
            question,
            conversation_id,
            include_all_files=True,
        )
        if include_all_files
        else search_uploaded_files(question, conversation_id)
    )
    has_active_upload = bool(uploaded and uploaded.has_active_file)
    deterministic_answer = uploaded.deterministic_answer if uploaded is not None else ""
    sql_trace = uploaded.sql_trace if uploaded is not None else ()
    if uploaded is not None and uploaded.file_context.strip():
        contexts.append(uploaded.file_context.strip())
        file_source_items = uploaded.file_source_items
    provided = (file_context or "").strip()
    if provided:
        contexts.append(provided)
    if not contexts:
        return None, (), has_active_upload, deterministic_answer, sql_trace
    combined = "\n\n".join(dict.fromkeys(contexts))
    if provided and len(combined) > COMBINED_FILE_CONTEXT_MAX_CHARS:
        raise InputSizeLimitError(
            field="combined_file_context",
            max_chars=COMBINED_FILE_CONTEXT_MAX_CHARS,
        )
    return (
        combined,
        file_source_items,
        has_active_upload,
        deterministic_answer,
        sql_trace,
    )


def _resolve_file_question(question: str, previous_turn: ConversationTurn | None) -> str:
    if is_ambiguous_file_analysis_question(question):
        return question
    if previous_turn is None:
        return question
    if _is_complete_ranked_file_question(question):
        return question
    previous = previous_turn.question.strip()
    resolved = question.strip()
    has_current_sheet = bool(re.search(r"[^\s]+\s*시트", question, re.IGNORECASE))
    has_current_measure = bool(
        re.search(
            r"(?:월별\s*(?:추이|흐름|변화|합계|금액|매출|집계)|sell[ -]?out|매출|금액|수량|단가|재구매율|\bq1\b|(?<![A-Za-z])no(?![A-Za-z])|VALUES\s+LC\s+SI\s+PRICE)",
            question,
            re.IGNORECASE,
        )
    )
    file_match = re.search(r"(?P<name>[^\s]+\.(?:xlsx?|xlsm|csv|pdf|docx?|pptx?))", previous, re.IGNORECASE)
    file_name = previous_turn.slots.file_name or (file_match.group("name") if file_match else "")
    if file_name and re.search(r"(?:이|해당|그)\s*문서", resolved):
        resolved = re.sub(r"(?:이|해당|그)\s*문서", file_name, resolved)
    elif (
        file_name
        and not has_current_sheet
        and not re.search(r"\.(?:xlsx?|xlsm|csv|pdf|docx?|pptx?)", resolved, re.IGNORECASE)
    ):
        resolved = f"{file_name}에서 {resolved}"

    manufacturers = tuple(
        dict.fromkeys(
            match.group("name")
            for match in re.finditer(
                r"(?P<name>[가-힣A-Za-z0-9_-]+(?:제약|약품))(?=(?:의|은|는|이|가|과|와|\s|[?.,!]|$))",
                question,
            )
        )
    )
    if len(manufacturers) == 1:
        manufacturer = manufacturers[0]
        resolved = re.sub(
            rf"{re.escape(manufacturer)}(?:의|은|는|이|가)?",
            f"{manufacturer}의",
            resolved,
            count=1,
        )
    elif (
        not manufacturers
        and not has_current_sheet
        and not has_current_measure
        and previous_turn.slots.file_manufacturer
    ):
        resolved = f"{previous_turn.slots.file_manufacturer}의 {resolved}"

    if not re.search(r"(?:합계|총계|합산|평균|개수|건수|집계|비교|총액|금액)", resolved):
        inherited = re.search(r"(?:합계|총계|합산|평균|개수|건수|집계|비교|총액|금액)", previous)
        if inherited:
            resolved = f"{resolved.rstrip('? ')} {inherited.group(0)}는?"
    file_measure = previous_turn.slots.file_measure or ""
    if (
        file_measure
        and not has_current_measure
        and not has_explicit_period_cue(question)
        and file_measure.casefold() not in resolved.casefold()
    ):
        resolved = f"{resolved.rstrip('? ')} {file_measure}는?"
    file_sheet = previous_turn.slots.file_sheet or ""
    if file_sheet and not has_current_sheet:
        resolved = f"{file_sheet} 시트에서 {resolved}"
    return resolved


def _is_complete_ranked_file_question(question: str) -> bool:
    """Keep an explicit ranked axis independent from stale turn slots."""

    return bool(
        re.search(
            r"(?:상위|하위)\s*\d+\s*(?:개\s*)?(?:제품|품목|브랜드|제조사|업체|채널)",
            question,
            re.IGNORECASE,
        )
    )


def _has_explicit_file_sheet_reference(question: str) -> bool:
    return bool(
        re.search(
            r"(?<![0-9A-Za-z가-힣_.-])[0-9A-Za-z가-힣_.-]+\s*시트(?:에서|의|는|은|이|가)?",
            question,
            re.IGNORECASE,
        )
    )


def _has_file_signal(documents: list[Path] | None, file_context: str | None) -> bool:
    return bool(documents) or bool((file_context or "").strip())


def _has_market_intent(question: str, *, has_brand_anchor: bool = False) -> bool:
    metric_signal = re.search(
        r"(?:시장|매출|실적|점유율|MS|순위|HHI|CR\d*|환자수|특허|영업활동|최근\s*\d*\s*(?:개월|년)?\s*추이)",
        question,
        re.IGNORECASE,
    )
    return (
        bool(metric_signal)
        or detect_market_scope_intent(question) is not None
        or should_use_agent_loop(question, has_brand_anchor=has_brand_anchor)
    )


def _is_entity_free_brand_metric_question(question: str) -> bool:
    normalized = re.sub(r"\s+", " ", question.strip()).rstrip("?!. ")
    patterns = (
        r"20\d{2}년?\s*(?:1[0-2]|0?[1-9])월\s*매출(?:\s*(?:얼마(?:야|인가요)?|알려줘))?",
        r"20\d{2}년?\s*[1-4]분기\s*매출(?:\s*(?:얼마(?:야|인가요)?|알려줘))?",
        r"(?:가장\s*)?최근\s*월\s*매출(?:\s*(?:얼마(?:야|인가요)?|알려줘))?",
        r"시장\s*점유율(?:\s*(?:변화|추이))?(?:\s*(?:설명해줘|알려줘))?",
    )
    return any(re.fullmatch(pattern, normalized, re.IGNORECASE) for pattern in patterns)


def _is_entity_free_market_metric_question(question: str) -> bool:
    normalized = re.sub(r"\s+", " ", question.strip()).rstrip("?!. ")
    patterns = (
        r"(?:시장\s*)?(?:상위\s*\d*\s*개?|top\s*\d+)\s*브랜드"
        r"(?:\s*(?:시장\s*)?점유율)?(?:\s*(?:알려줘|보여줘))?",
        r"(?:시장\s*)?(?:HHI|CR5|집중도)(?:\s*(?:알려줘|보여줘))?",
        r"시장\s*규모(?:\s*(?:알려줘|보여줘))?",
    )
    return any(re.fullmatch(pattern, normalized, re.IGNORECASE) for pattern in patterns)


def _ground_unanchored_market_golden(
    question: str,
    *,
    has_explicit_anchor: bool,
) -> str:
    """Bind only established standalone market contracts to their strategic anchor."""

    if has_explicit_anchor or has_file_reference(question):
        return question
    top_n = re.fullmatch(
        r"(?:고지혈증|이상지질혈증)(?:\s*시장)?\s*상위\s*(\d+)\s*개?"
        r"(?:\s*브랜드)?(?:\s*(?:알려줘|보여줘))?[?!.]?|"
        r"(?:고지혈증|이상지질혈증)(?:\s*시장)?\s*top\s*(\d+)"
        r"(?:\s*브랜드)?(?:\s*(?:알려줘|보여줘))?[?!.]?",
        question.strip(),
        re.IGNORECASE,
    )
    if top_n:
        limit = top_n.group(1) or top_n.group(2)
        return f"리바로 시장 상위 {limit}개와 HHI, CR5를 알려줘"
    if re.fullmatch(
        r"(?:고지혈증|이상지질혈증)(?:\s*시장)?\s*(?:HHI|집중도)(?:\s*(?:알려줘|보여줘))?[?!.]?",
        question.strip(),
        re.IGNORECASE,
    ):
        return "리바로 시장 HHI와 CR5를 알려줘"
    if re.fullmatch(
        r"(?:고지혈증|이상지질혈증)\s*시장\s*상황(?:\s*알려\s*줘)?[?!.]?",
        question.strip(),
        re.IGNORECASE,
    ):
        return "리바로 시장 상황 알려줘"
    if re.fullmatch(
        r"(?:고지혈증|이상지질혈증)(?:\s*시장)?\s*최근\s*이슈와\s*시장\s*변화[?!.]?",
        question.strip(),
        re.IGNORECASE,
    ):
        return "리바로 시장 최근 이슈와 시장 변화"
    return question


def _uses_monthly_market_golden(question: str, grounded_question: str) -> bool:
    """Identify the approved monthly HHI/top-brand contracts behind a synthetic anchor."""

    return grounded_question != question and (
        grounded_question.startswith("리바로 시장 상위 ")
        or grounded_question == "리바로 시장 HHI와 CR5를 알려줘"
    )


def _file_scoped_result(question: str) -> dict:
    answer = "업로드 파일에서 확인된 근거만 사용해 답변합니다."
    return {
        "question": question,
        "answer": answer,
        "sources": ["document"],
        "tool_calls": [],
        "resolution": {"scope": ContextScope.FILE.value},
        "router_diagnostics": {"mode": "file_context_scope_lock", "deterministic_execution": True},
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
    }


def _enforce_file_scope_isolation(result: dict, question: str, scope: ContextScope) -> dict:
    if scope is not ContextScope.FILE:
        return result
    market_tools = {
        "get_brand_metric",
        "get_brand_series",
        "get_market_scope",
        "get_market_top_brands",
        "general_view_unavailable",
    }
    calls = result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else []
    contaminated = any(
        str(call.get("tool") or "") in market_tools
        for call in calls
        if isinstance(call, dict)
    )
    sources = {str(value) for value in result.get("sources", ()) if value}
    if contaminated or any(value not in {"document", "file_upload"} for value in sources):
        return _file_scoped_result(question)
    return result


_FILE_MARKET_POSTPROCESS_RE = re.compile(
    r"(?:시장\s*도구\s*미호출|일반뷰\s*(?:브랜드\s*)?(?:비교|조회)|"
    r"시장\s*지표\s*(?:도구|조회))",
    re.IGNORECASE,
)


def _enforce_file_postprocess_isolation(answer: str, result: dict) -> str:
    """Reject market-contract prose introduced after a FILE-scoped execution."""

    if result.get("context_scope") != ContextScope.FILE.value:
        return answer
    calls = result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else []
    if calls or not _FILE_MARKET_POSTPROCESS_RE.search(answer):
        return answer
    deterministic = str(result.get("deterministic_file_answer") or "").strip()
    if deterministic:
        return deterministic
    context = str(result.get("file_context") or "").strip()
    if context:
        return _file_context_fallback_answer("## 업로드 파일 컨텍스트\n" + context)
    return "업로드 파일 SQL 결과를 확인하지 못했습니다. 파일의 열과 조건을 확인해 다시 질문해 주세요."


def _annotate_context_scope(result: dict, scope: ContextScope) -> dict:
    copied = dict(result)
    copied["context_scope"] = scope.value
    markdown = copied.get("markdown_response")
    if isinstance(markdown, dict):
        copied["markdown_response"] = {**markdown, "context_scope": scope.value}
    return copied


def _file_only_ready_result(
    documents: list[Path] | None,
    file_context: str | None,
    *,
    file_overviews: tuple[UploadedFileOverview, ...] = (),
) -> dict:
    file_names = list(
        dict.fromkeys(
            [path.name for path in documents or []]
            + [overview.file_name for overview in file_overviews]
        )
    )
    count = len(file_names)
    count_text = f"{count}개" if count else ""
    subject = f"파일 {count_text}".strip()
    answer = f"{subject} 저장 완료했습니다. 이 세션에서 질문하면 업로드한 파일을 참조해 답변합니다."
    if file_overviews:
        answer = "파일 확인 완료 - 지금 질문하실 수 있어요."
        answer = f"{answer}\n\n" + "\n\n".join(
            render_uploaded_file_machine_brief(overview) for overview in file_overviews
        )
    elif file_names:
        answer = f"{answer}\n\n" + "\n".join(f"- {name}" for name in file_names)
    if file_context and not file_names:
        answer = f"{answer}\n\n- 업로드 파일"
    cleaned_answer = cleanup_markdown_answer(answer)
    grounding_text = "\n".join(
        value
        for value in (
            cleaned_answer,
            render_file_brief_grounding_text(file_overviews),
        )
        if value
    )
    return {
        "answer": cleaned_answer,
        "sources": ["file_upload"],
        "tool_calls": [],
        "file_only_ready": True,
        "file_names": file_names,
        "file_brief_basis": "observed_schema" if file_overviews else "file_name",
        "file_brief_is_answer_evidence": False,
        "file_brief_observed": serialize_file_overviews(file_overviews),
        "file_brief_grounding_text": grounding_text,
    }

def _attach_file_context(
    result: dict, file_context: str | None, file_source_items: tuple[dict[str, Any], ...] = ()
) -> dict:
    context = (file_context or "").strip()
    if not context:
        return result
    copied = dict(result)
    copied["file_context"] = context
    if file_source_items:
        copied["file_source_items"] = [dict(item) for item in file_source_items]
    sources = [str(source) for source in copied.get("sources", []) if source]
    if "document" not in sources:
        sources.append("document")
    copied["sources"] = sources
    return copied


def _file_context_fact(result: dict) -> str:
    value = result.get("file_context")
    if not isinstance(value, str):
        return ""
    context = value.strip()
    if not context:
        return ""
    return "## 업로드 파일 컨텍스트\n" + context


def _append_file_context_source(answer: str, fact_md: str, file_context_fact: str) -> str:
    if not file_context_fact:
        return answer
    return append_deterministic_source_block(answer, fact_md, file_context=file_context_fact)


def _looks_like_empty_file_context_answer(answer: str) -> bool:
    stripped = answer.strip()
    if not stripped:
        return True
    empty_markers = (
        "표시할 검증 fact가 제한적",
        "표시할 확정 fact가 없습니다",
        "검증 fact가 제한적입니다",
    )
    return any(marker in stripped for marker in empty_markers)


def _file_context_fallback_answer(file_context_fact: str) -> str:
    context = file_context_fact.removeprefix("## 업로드 파일 컨텍스트").strip()
    return cleanup_markdown_answer(
        "업로드 파일 기준으로 확인된 내용입니다.\n\n"
        + context
    )


def _answer_with_conversation(
    store: SessionStore,
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    conversation_id: str,
    question: str,
    external_mode: str,
    documents: list[Path] | None,
    *,
    use_direct_agent_loop: bool = False,
) -> dict:
    pending = store.conversations.get_pending(conversation_id)
    if pending is not None and pending.kind == "hira_disease_code":
        selected = _select_hira_disease_candidate(question, pending.disease_candidates)
        if selected is not None:
            store.conversations.clear_pending(conversation_id)
            resumed_question = f"질병코드 {selected.sick_cd} 기준으로 {pending.original_question}"
            return _answer_without_pending(
                market_scope_resolver,
                agent_factory,
                conversation_id,
                resumed_question,
                external_mode,
                documents,
                store,
                use_direct_agent_loop=use_direct_agent_loop,
            )
        explicit_code = explicit_hira_disease_code(question)
        if explicit_code is not None:
            store.conversations.clear_pending(conversation_id)
            resumed_question = f"질병코드 {explicit_code} 기준으로 {pending.original_question}"
            return _answer_without_pending(
                market_scope_resolver,
                agent_factory,
                conversation_id,
                resumed_question,
                external_mode,
                documents,
                store,
                use_direct_agent_loop=use_direct_agent_loop,
            )
    if pending is not None and pending.kind == "brand_metric":
        store.conversations.clear_pending(conversation_id)
        if _is_brand_metric_clarification_reply(question, market_scope_resolver):
            resumed_question = f"{question.strip()} {pending.original_question}"
            return _answer_without_pending(
                market_scope_resolver,
                agent_factory,
                conversation_id,
                resumed_question,
                external_mode,
                documents,
                store,
                use_direct_agent_loop=use_direct_agent_loop,
            )
        result = _answer_without_pending(
            market_scope_resolver,
            agent_factory,
            conversation_id,
            question,
            external_mode,
            documents,
            store,
            use_direct_agent_loop=use_direct_agent_loop,
        )
        return _prepend_brand_pending_notice(result)
    if pending is not None and pending.kind == "market_view":
        view_type = map_market_view_reply(question)
        store.conversations.clear_pending(conversation_id)
        if view_type is not None:
            return market_scope_resolver.answer(pending.original_question, view_type=view_type)
        result = _answer_without_pending(
            market_scope_resolver,
            agent_factory,
            conversation_id,
            question,
            external_mode,
            documents,
            store,
            use_direct_agent_loop=use_direct_agent_loop,
        )
        return _prepend_pending_notice(result)
    state = store.conversations.get_or_create(conversation_id)
    previous_turn = state.turns[-1] if state.turns else None
    resolution = resolve_anaphora(
        question,
        previous_turn,
        known_brand=getattr(market_scope_resolver, "has_explicit_brand_anchor", None),
    )
    if resolution.unresolved_reference:
        return unresolved_reference_result(question)
    if resolution.reusable_ranked is not None:
        return reused_context_result(question, resolution.reusable_ranked, previous_turn.slots if previous_turn else None)
    if _is_disease_candidate_only_reply(question):
        return unresolved_reference_result(question)
    result = _answer_without_pending(
        market_scope_resolver,
        agent_factory,
        conversation_id,
        resolution.resolved_question,
        external_mode,
        documents,
        store,
        use_direct_agent_loop=use_direct_agent_loop,
        # A cause question inherits the observation the previous turn showed. Passed
        # beside the question rather than folded into it, so the planner still selects
        # its contract from the words the user actually typed.
        issue_context=resolution.inherited_issue_observation,
    )
    _store_hira_disease_candidates(store, conversation_id, resolution.resolved_question, result)
    if resolution.interpretation_notice:
        return {**result, "conversation_interpretation": resolution.interpretation_notice}
    return result


_DISEASE_ORDINALS = {
    "첫번째": 0,
    "첫째": 0,
    "1번": 0,
    "두번째": 1,
    "둘째": 1,
    "2번": 1,
    "세번째": 2,
    "셋째": 2,
    "3번": 2,
    "네번째": 3,
    "넷째": 3,
    "4번": 3,
    "다섯번째": 4,
    "다섯째": 4,
    "5번": 4,
}

_DISEASE_TYPE_REFERENCES = frozenset(
    {
        "1형",
        "2형",
        "3형",
        "제1형",
        "제2형",
        "제3형",
    }
)


def _is_disease_candidate_only_reply(reply: str) -> bool:
    normalized = re.sub(r"\s+", "", reply.strip()).casefold()
    return normalized in _DISEASE_ORDINALS or normalized in _DISEASE_TYPE_REFERENCES


def _select_hira_disease_candidate(
    reply: str,
    candidates: tuple[DiseaseCodeCandidateSlot, ...],
) -> DiseaseCodeCandidateSlot | None:
    normalized = re.sub(r"\s+", "", reply.strip()).casefold()
    if not normalized or not candidates:
        return None
    for candidate in candidates:
        code = candidate.sick_cd.casefold()
        if normalized in {code, code.replace(".", "")}:
            return candidate
    ordinal = _DISEASE_ORDINALS.get(normalized)
    if ordinal is not None and ordinal < len(candidates):
        return candidates[ordinal]
    if len(normalized) < 2:
        return None
    name_matches = tuple(
        candidate
        for candidate in candidates
        if normalized in re.sub(r"\s+", "", candidate.disease_name).casefold()
    )
    return name_matches[0] if len(name_matches) == 1 else None


def _store_hira_disease_candidates(
    store: SessionStore,
    conversation_id: str,
    original_question: str,
    result: Mapping[str, Any],
) -> None:
    raw_calls = result.get("tool_calls")
    if not isinstance(raw_calls, list):
        return
    candidate_call = next(
        (
            call
            for call in raw_calls
            if isinstance(call, dict) and call.get("tool") == "hira_disease_code_ambiguous"
        ),
        None,
    )
    if candidate_call is None:
        return
    render_data = candidate_call.get("render_data")
    raw_candidates = render_data.get("candidates") if isinstance(render_data, dict) else None
    if not isinstance(raw_candidates, list):
        return
    candidates = tuple(
        DiseaseCodeCandidateSlot(
            sick_cd=str(candidate["sickCd"]).strip().upper(),
            disease_name=str(candidate["sickNm"]).strip(),
        )
        for candidate in raw_candidates
        if isinstance(candidate, dict)
        and isinstance(candidate.get("sickCd"), str)
        and candidate["sickCd"].strip()
        and isinstance(candidate.get("sickNm"), str)
        and candidate["sickNm"].strip()
    )
    if not candidates:
        return
    expires_at = store.conversations.pending_expiry()
    store.conversations.set_pending(
        conversation_id,
        PendingClarification(
            kind="hira_disease_code",
            original_question=original_question,
            brand="",
            metric="patient_count",
            created_at=expires_at - store.conversations.pending_ttl_seconds,
            expires_at=expires_at,
            disease_candidates=candidates,
        ),
    )


def _answer_without_pending(
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    conversation_id: str,
    question: str,
    external_mode: str,
    documents: list[Path] | None,
    store: SessionStore,
    *,
    use_direct_agent_loop: bool = False,
    issue_context: tuple[str, ...] = (),
) -> dict:
    route_method = getattr(market_scope_resolver, "general_route", None)
    with stage(None, "question_classification", "view selection"):
        route = route_method(question) if callable(route_method) else GeneralRoute.EXISTING
    if route is GeneralRoute.GENERAL_ONLY:
        return market_scope_resolver.answer_general(question, compact=False, dual=False)
    if route is GeneralRoute.DUAL:
        return _answer_dual_view(
            market_scope_resolver,
            agent_factory,
            conversation_id,
            question,
            external_mode,
            documents,
            store,
            use_direct_agent_loop=use_direct_agent_loop,
            issue_context=issue_context,
        )
    return _answer_existing_without_pending(
        market_scope_resolver,
        agent_factory,
        conversation_id,
        question,
        external_mode,
        documents,
        store,
        use_direct_agent_loop=use_direct_agent_loop,
        issue_context=issue_context,
    )


def _answer_dual_view(
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    conversation_id: str,
    question: str,
    external_mode: str,
    documents: list[Path] | None,
    store: SessionStore,
    *,
    use_direct_agent_loop: bool,
    issue_context: tuple[str, ...] = (),
) -> dict:
    strategic_question = f"{question}\n\n전략뷰(market_landscape) 기준으로 주 답변을 작성하세요."
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="general-view") as executor:
        general_future = executor.submit(
            market_scope_resolver.answer_general,
            question,
            compact=True,
            dual=True,
        )
        strategic = _answer_existing_without_pending(
            market_scope_resolver,
            agent_factory,
            conversation_id,
            strategic_question,
            external_mode,
            documents,
            store,
            use_direct_agent_loop=use_direct_agent_loop,
            issue_context=issue_context,
        )
        general = general_future.result()
    combined = dict(strategic)
    combined["question"] = question
    combined["general_view_contract"] = general.get("general_view_contract")
    combined["tool_calls"] = [*strategic.get("tool_calls", []), *general.get("tool_calls", [])]
    combined["sources"] = list(dict.fromkeys([*strategic.get("sources", []), *general.get("sources", [])]))
    diagnostics = dict(strategic.get("router_diagnostics") or {})
    diagnostics["general_view_mode"] = "dual"
    combined["router_diagnostics"] = diagnostics
    return combined


def _answer_existing_without_pending(
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    conversation_id: str,
    question: str,
    external_mode: str,
    documents: list[Path] | None,
    store: SessionStore,
    *,
    use_direct_agent_loop: bool = False,
    issue_context: tuple[str, ...] = (),
) -> dict:
    # Only forwarded when the previous turn actually left an observation, so every
    # question without one reaches the agent through the call it always used.
    agent_kwargs: dict[str, Any] = {"issue_context": issue_context} if issue_context else {}

    def observe_market_route(
        handler: str,
        *,
        reason: str,
        mode: RouteMode = RouteMode.DETERMINISTIC,
        domain: str = "market",
    ) -> None:
        observe_route_decision(
            question=question,
            domain=domain,
            handler=handler,
            mode=mode,
            decided_by="market_shortcut",
            reason_codes=(reason,),
            rejected_alternatives=(
                RejectedRoute(
                    domain="market",
                    handler=(
                        "agent_loop" if mode is RouteMode.DETERMINISTIC else "market_shortcut"
                    ),
                    reason_codes=(
                        (
                            "shortcut_selected"
                            if mode is RouteMode.DETERMINISTIC
                            else "shortcut_not_selected"
                        ),
                    ),
                ),
            ),
        )
        observe_unified_market_shortcut_shadow(
            question=question,
            has_documents=bool(documents),
            use_direct_agent_loop=use_direct_agent_loop,
            market_scope_resolver=market_scope_resolver,
            legacy_domain=domain,
            legacy_handler=handler,
            legacy_mode=mode,
        )

    decision_kwargs = {
        "question": question,
        "has_documents": bool(documents),
        "use_direct_agent_loop": use_direct_agent_loop,
        "market_scope_resolver": market_scope_resolver,
        "should_use_agent_loop_fn": should_use_agent_loop,
        "requested_unavailable_source_fn": requested_unavailable_source,
        "asks_market_members_fn": asks_market_members,
        "detect_market_scope_intent_fn": detect_market_scope_intent,
        "market_scope_defers_to_contract_fn": market_scope_defers_to_contract,
        "tool_use_requirements_fn": tool_use_requirements,
        "v4_enforces_external_question_fn": _v4_enforces_external_question,
        "requested_period_fn": requested_period,
    }
    decision = (
        decide_market_shortcut(**decision_kwargs)
        if routing_boundaries_enabled()
        else _legacy_market_shortcut_decision(**decision_kwargs)
    )
    canonical_cutover = _hira_reimbursement_cutover_decision(
        question=question,
        has_documents=bool(documents),
        use_direct_agent_loop=use_direct_agent_loop,
        market_scope_resolver=market_scope_resolver,
    )
    if canonical_cutover is not None:
        canonical_result = _answer_hira_reimbursement_cutover(question, external_mode)
        if canonical_result is not None:
            observe_market_route(
                canonical_cutover.handler,
                reason="canonical_hira_reimbursement_cutover",
                domain=canonical_cutover.domain,
            )
            diagnostics = dict(canonical_result.get("router_diagnostics") or {})
            diagnostics["canonical_router_cutover"] = {
                "domain": canonical_cutover.domain,
                "handler": canonical_cutover.handler,
                "mode": canonical_cutover.execution_mode.value,
            }
            canonical_result["router_diagnostics"] = diagnostics
            return canonical_result
    disease_stats_cutover = _hira_disease_stats_cutover_decision(
        question=question,
        has_documents=bool(documents),
        use_direct_agent_loop=use_direct_agent_loop,
        market_scope_resolver=market_scope_resolver,
    )
    if disease_stats_cutover is not None:
        canonical_result = _answer_hira_disease_stats_cutover(
            agent_factory,
            question,
            external_mode,
            agent_kwargs,
        )
        if canonical_result is not None:
            observe_market_route(
                disease_stats_cutover.handler,
                reason="canonical_hira_disease_stats_cutover",
                domain=disease_stats_cutover.domain,
            )
            diagnostics = dict(canonical_result.get("router_diagnostics") or {})
            diagnostics["canonical_router_cutover"] = {
                "domain": disease_stats_cutover.domain,
                "handler": disease_stats_cutover.handler,
                "mode": disease_stats_cutover.execution_mode.value,
            }
            canonical_result["router_diagnostics"] = diagnostics
            return canonical_result
    mfds_cutover = _mfds_cutover_decision(
        question=question,
        has_documents=bool(documents),
        use_direct_agent_loop=use_direct_agent_loop,
        market_scope_resolver=market_scope_resolver,
    )
    if mfds_cutover is not None:
        canonical_result = _answer_mfds_cutover(question, external_mode)
        if canonical_result is not None:
            observe_market_route(
                mfds_cutover.handler,
                reason="canonical_mfds_cutover",
                domain=mfds_cutover.domain,
            )
            diagnostics = dict(canonical_result.get("router_diagnostics") or {})
            diagnostics["canonical_router_cutover"] = {
                "domain": mfds_cutover.domain,
                "handler": mfds_cutover.handler,
                "mode": mfds_cutover.execution_mode.value,
            }
            canonical_result["router_diagnostics"] = diagnostics
            return canonical_result
    clinical_trials_cutover = _clinical_trials_cutover_decision(
        question=question,
        has_documents=bool(documents),
        use_direct_agent_loop=use_direct_agent_loop,
        market_scope_resolver=market_scope_resolver,
    )
    if clinical_trials_cutover is not None:
        canonical_result = _answer_clinical_trials_cutover(
            agent_factory,
            question,
            external_mode,
            agent_kwargs,
        )
        if canonical_result is not None:
            observe_market_route(
                clinical_trials_cutover.handler,
                reason="canonical_clinical_trials_cutover",
                mode=clinical_trials_cutover.execution_mode,
                domain=clinical_trials_cutover.domain,
            )
            diagnostics = dict(canonical_result.get("router_diagnostics") or {})
            diagnostics["canonical_router_cutover"] = {
                "domain": clinical_trials_cutover.domain,
                "handler": clinical_trials_cutover.handler,
                "mode": clinical_trials_cutover.execution_mode.value,
            }
            canonical_result["router_diagnostics"] = diagnostics
            return canonical_result
    if decision.kind is MarketRouteKind.EXPLICIT_MARKET_ID:
        observe_market_route(decision.handler, reason=decision.reason)
        return market_scope_resolver.answer_market_id(
            question,
            market_id=decision.market_id or "",
            period=decision.period or "latest",
        )
    if decision.kind is MarketRouteKind.REQUESTED_SOURCE_AGENT:
        with stage(None, "question_classification", "agent setup"):
            agent = agent_factory(external_mode=external_mode)
        observe_market_route(
            decision.handler,
            reason=decision.reason,
            mode=RouteMode.AGENTIC,
        )
        return agent.answer(question, documents, **agent_kwargs)
    if decision.kind is MarketRouteKind.MARKET_MEMBERS_BRAND:
        observe_market_route(decision.handler, reason=decision.reason)
        return market_scope_resolver.answer(question, view_type="market_landscape")
    if decision.kind is MarketRouteKind.NAMED_MARKET:
        observe_market_route(decision.handler, reason=decision.reason)
        return market_scope_resolver.answer_named_market(question)
    if decision.kind is MarketRouteKind.DIRECT_AGENT_LOOP:
        observe_market_route(
            decision.handler,
            reason=decision.reason,
            mode=RouteMode.AGENTIC,
        )
        return _answer_direct_agent_loop(question, external_mode, **agent_kwargs)
    if decision.kind is MarketRouteKind.AGENT_LOOP:
        with stage(None, "question_classification", "agent setup"):
            agent = agent_factory(external_mode=external_mode)
        observe_market_route(
            decision.handler,
            reason=decision.reason,
            mode=RouteMode.AGENTIC,
        )
        return agent.answer(question, documents, **agent_kwargs)
    intent = decision.intent
    if decision.kind is MarketRouteKind.MARKET_CLARIFICATION and intent is not None:
        brand = intent.brand_hint or "해당 브랜드"
        expires_at = store.conversations.pending_expiry()
        pending = PendingClarification(
            kind="market_view",
            original_question=question,
            brand=brand,
            metric=intent.metric,
            created_at=expires_at - store.conversations.pending_ttl_seconds,
            expires_at=expires_at,
        )
        store.conversations.set_pending(conversation_id, pending)
        observe_market_route(decision.handler, reason=decision.reason)
        return market_scope_resolver.clarification(question, brand=brand)
    if decision.kind is MarketRouteKind.MARKET_SCOPE_ANSWER and intent is not None:
        observe_market_route(decision.handler, reason=decision.reason)
        return market_scope_resolver.answer(question, view_type=intent.view_type)
    raise AssertionError(f"unhandled market routing decision: {decision.kind}")


def _answer_direct_agent_loop(
    question: str,
    external_mode: str,
    *,
    issue_context: tuple[str, ...] = (),
) -> dict:
    # Only forwarded when the previous turn actually left an observation, so an agent
    # loop that predates the parameter keeps being called exactly as before.
    agent_kwargs: dict[str, Any] = {"issue_context": issue_context} if issue_context else {}
    with stage(None, "question_classification", "agent setup"):
        dependencies = build_chat_agent_dependencies(external_mode=external_mode)
    routes = ()
    structured_plan = None
    with trace_span("structured_preflight", "deterministic structured question preflight"):
        if callable(getattr(dependencies.resolver, "resolve_many", None)):
            try:
                structured_plan = preflight_structured_market_question(question, dependencies.resolver)
            except AmbiguousBrandError as exc:
                result = ambiguous_brand_result(
                    question,
                    routes,
                    router_diagnostics(dependencies.router),
                    exc.candidates,
                )
                return attach_routing_v4_legacy_observation(
                    question,
                    result,
                    resolver=dependencies.resolver,
                    external=getattr(dependencies, "external", None),
                )
    if (
        not is_explicit_quarter_sales_question(question)
        and structured_plan is None
    ):
        with stage(None, "question_decomposition", "BQ and tool routing"):
            routes = dependencies.router.route(question, has_documents=False)
    with trace_span("metric_owner_resolution", "structured metric owner classification"):
        metric_owner = structured_metric_owner(question)
    skip_single_brand_resolution = metric_owner == "market" and structured_plan is None
    with trace_span("canonical_brand_resolution", "canonical brand validation"):
        if (
            not skip_single_brand_resolution
            and not is_portfolio_decline_question(question, routes)
            and not _is_known_ingredient_patent_question(question)
        ):
            try:
                dependencies.resolver.resolve(question, allow_default=False)
            except AmbiguousBrandError as exc:
                result = ambiguous_brand_result(
                    question,
                    routes,
                    router_diagnostics(dependencies.router),
                    exc.candidates,
                )
                return attach_routing_v4_legacy_observation(
                    question,
                    result,
                    resolver=dependencies.resolver,
                    external=getattr(dependencies, "external", None),
                )
            except UnsupportedBrandError:
                typed_result = (
                    unsupported_hira_interface_result
                    if is_hira_disease_question(question)
                    else unsupported_brand_result
                )
                result = typed_result(question, routes, router_diagnostics(dependencies.router))
                return attach_routing_v4_legacy_observation(
                    question,
                    result,
                    resolver=dependencies.resolver,
                    external=getattr(dependencies, "external", None),
                )
    with trace_span("agent_loop_construction", "tool-use agent construction"):
        agent = build_tool_use_agent(dependencies.agent_loop_dependencies())
    with trace_span("agent_loop_execution", "tool-use agent execution"):
        try:
            result = agent.answer(question, **agent_kwargs)
        except BrandUnresolvedError as exc:
            # The planner's own message is "ask the user to specify a brand", so
            # asking is what it already wanted; until now the request died as an
            # ASGI 500 instead, which also skipped compute_final_answer and left
            # no qa_trace to diagnose from. Returning a result keeps the normal
            # answer path, so the reason reaches the user and the trace exists.
            result = brand_unresolved_result(
                question,
                routes,
                router_diagnostics(dependencies.router),
                message=(
                    multi_brand_cardinality_message(exc.matches)
                    if len(exc.matches) >= 2
                    else None
                ),
            )
    return attach_routing_v4_legacy_observation(
        question,
        result,
        resolver=dependencies.resolver,
        external=getattr(dependencies, "external", None),
    )


def _v4_enforces_external_question(question: str) -> bool:
    return (
        configured_routing_mode() is RoutingMode.ENFORCE
        and classify_question(question).source_domain
        in {"hira", "regulatory", "clinical_trials"}
    )


def _answer_deep_research(question: str, external_mode: str) -> dict:
    with stage(None, "deep_research_prepare", "브랜드와 조사 범위 확인"):
        dependencies = build_chat_agent_dependencies(external_mode=external_mode)
        try:
            dependencies.resolver.resolve(question, allow_default=False)
        except AmbiguousBrandError as exc:
            result = ambiguous_brand_result(
                question,
                [],
                {
                    "mode": "deep_research",
                    "deterministic_execution": True,
                    "model": "gemini-3.1-pro-preview",
                    "serving_id": "202",
                },
                exc.candidates,
            )
            return {**result, "research_mode": "deep", "effective_question": question}
        except UnsupportedBrandError:
            typed_result = (
                unsupported_hira_interface_result
                if is_hira_disease_question(question)
                else unsupported_brand_result
            )
            result = typed_result(
                question,
                [],
                {
                    "mode": "deep_research",
                    "deterministic_execution": True,
                    "model": "gemini-3.1-pro-preview",
                    "serving_id": "202",
                },
            )
            return {**result, "research_mode": "deep", "effective_question": question}

    agent = ToolUseAgent(
        metrics=dependencies.metrics,
        resolver=dependencies.resolver,
        planner=DeepResearchToolPlanner(),
        max_steps=2,
        news=dependencies.news,
        external=dependencies.external,
        query_layer=dependencies.query_layer,
        progress_namespace="deep",
    )
    result = agent.answer(question)
    diagnostics = dict(result.get("router_diagnostics") or {})
    timing = result.get("timing")
    stages = timing.get("stages") if isinstance(timing, dict) else ()
    parallel_tool_count = sum(
        1
        for item in stages
        if isinstance(item, dict)
        and str(item.get("name") or "").startswith("tool:")
        and "mode=parallel" in str(item.get("detail") or "")
    )
    diagnostics.update(
        {
            "mode": "deep_research",
            "deterministic_execution": True,
            "model": "gemini-3.1-pro-preview",
            "serving_id": "202",
            "tool_execution_mode": "parallel" if parallel_tool_count >= 2 else "serial",
            "parallel_tool_count": parallel_tool_count,
        }
    )
    return {
        **result,
        "research_mode": "deep",
        "effective_question": question,
        "router_diagnostics": diagnostics,
    }


def _deep_research_clarification_result() -> dict[str, Any]:
    answer = "딥리서치할 질문을 `/deep` 뒤에 입력해 주세요. 예: `/deep 리바로 경쟁구도 분석`"
    return {
        "answer": answer,
        "sources": [],
        "tool_calls": [],
        "markdown_response": {"markdown": answer, "fact_md": "", "data_md": ""},
        "router_diagnostics": {
            "mode": "deep_research",
            "deterministic_execution": True,
            "model": "gemini-3.1-pro-preview",
            "serving_id": "202",
        },
    }


def _is_known_ingredient_patent_question(question: str) -> bool:
    lower = question.lower()
    asks_patent = "특허" in question or "patent" in lower or "orange" in lower
    return asks_patent and resolve_patent_ingredient_query(question) is not None


def _prepend_pending_notice(result: dict) -> dict:
    copied = dict(result)
    notice = "이전 시장 기준 선택 요청은 이번 답변과 매칭되지 않아 새 질문으로 처리했습니다.\n\n"
    copied["answer"] = notice + str(result.get("answer") or "")
    return copied


def _prepend_brand_pending_notice(result: dict) -> dict:
    copied = dict(result)
    notice = "이전 브랜드 확인 요청과 매칭되지 않아 새 질문으로 처리했습니다.\n\n"
    copied["answer"] = notice + str(result.get("answer") or "")
    return copied


def _is_brand_metric_clarification_reply(
    question: str,
    market_scope_resolver: MarketScopeResolver,
) -> bool:
    if not market_scope_resolver.has_explicit_anchor(question):
        return False
    return re.fullmatch(
        r"\s*(?:그럼\s+)?[가-힣A-Za-z0-9+_-]{2,40}(?:은|는|으로|로)?\s*[?.!。？！]*\s*",
        question,
    ) is not None


def _applied_filters(result: dict) -> tuple[tuple[str, str], ...]:
    filters: list[tuple[str, str]] = []
    for call in result.get("tool_calls", []):
        if not isinstance(call, dict):
            continue
        data = call.get("render_data")
        if isinstance(data, dict):
            for key in ("metric", "view_type", "scope"):
                value = data.get(key)
                if value is not None:
                    filters.append((key, str(value)))
    return tuple(filters)


def _default_agent_factory(*, external_mode: str = "live") -> ChatAgent:
    return ChatAgent(external_mode=external_mode)


def _stream_resolving_session_events(
    store: SessionStore,
    market_scope_resolver: MarketScopeResolver,
    agent_factory: AgentFactory,
    question: str,
    external_mode: str,
    conversation_id: str | None,
    *,
    use_direct_agent_loop: bool = False,
    history_store: ConversationHistoryStore | None = None,
    projection_context: ProjectionRequestContext | None = None,
    limiter: ChatConcurrencyLimiter | None = None,
):
    events: queue.Queue[dict[str, Any]] = queue.Queue()
    step_index = 0
    step_index_lock = threading.Lock()
    acquire_finished = threading.Event()
    acquired = False

    def indexed_step(event: dict[str, Any]) -> dict[str, Any]:
        nonlocal step_index
        with step_index_lock:
            step_index += 1
            index = step_index
        item = dict(event)
        item["index"] = index
        return item

    def emit_step(event: dict[str, Any]) -> None:
        public_event = dict(event)
        public_event.pop("raw_name", None)
        public_event.pop("raw_detail", None)
        if public_event.get("summary"):
            public_event["summary"] = public_stage_summary(str(public_event["summary"]))
        item = indexed_step(public_event)
        events.put({"type": "step", "item": item})

    def run_worker() -> None:
        nonlocal acquired
        try:
            acquired = limiter is None or limiter.try_acquire()
            acquire_finished.set()
            if not acquired:
                events.put({"type": "busy"})
                return
            emit_step({"name": "질문 접수", "detail": "요청 처리 시작", "status": "started", "raw_name": "question_received", "raw_detail": "request accepted"})
            emit_step({"name": "질문 접수", "detail": "요청 처리 시작", "status": "done", "raw_name": "question_received", "raw_detail": "request accepted", "elapsed_ms": 0.0})
            deep_request = parse_deep_research_request(question)
            total_stage = "deep_research_total" if deep_request.enabled else "answer_generation_total"
            total_detail = "딥리서치 전체 진행" if deep_request.enabled else "request processing"
            with stage_event_sink(emit_step):
                with stage(None, total_stage, total_detail):
                    item = _answer_question(
                        store,
                        market_scope_resolver,
                        agent_factory,
                        question,
                        external_mode,
                        conversation_id,
                        use_direct_agent_loop=use_direct_agent_loop,
                        timing_sink=emit_step,
                        conversation_history=history_store,
                    )
                    streamed_prefix = _stream_ready_prefix(item["result"])
                    if streamed_prefix:
                        prefix_emitted = threading.Event()
                        events.put(
                            {
                                "type": "file_ready",
                                "conversation_id": item.get("conversation_id"),
                                "sources": tuple(item["result"].get("sources", ())),
                                "text": streamed_prefix,
                                "emitted": prefix_emitted,
                            }
                        )
                        prefix_emitted.wait(timeout=2.0)
                    with shadow_request_id_scope(
                        getattr(item, "shadow_request_id", "")
                    ):
                        final_answer = _compute_final_answer_with_query_spec(
                            item["question"],
                            item["result"],
                            item.get("conversation_id"),
                            getattr(
                                item,
                                "operation_contract_query_spec",
                                None,
                            ),
                        )
            _record_conversation_history(
                history_store,
                session_id=None,
                question=item["question"],
                final_answer=final_answer,
                projection_context=projection_context,
            )
            events.put(
                {
                    "type": "result",
                    "item": item,
                    "final_answer": final_answer,
                    "streamed_prefix": streamed_prefix,
                }
            )
        except Exception as exc:
            acquire_finished.set()
            events.put({"type": "error", "error_type": type(exc).__name__, "message": str(exc)})
        finally:
            if limiter is not None and acquired:
                limiter.release()

    thread = threading.Thread(target=run_worker, name="chat-stream-answer-worker", daemon=True)
    try:
        thread.start()
    except Exception:
        raise
    wait_started = time.perf_counter()
    next_wait_progress = QUEUE_PROGRESS_THRESHOLD_S
    while True:
        try:
            waited = time.perf_counter() - wait_started
            poll_timeout = min(0.25, max(0.01, next_wait_progress - waited))
            event = events.get(timeout=poll_timeout)
        except queue.Empty:
            waited = time.perf_counter() - wait_started
            if not acquire_finished.is_set() and waited >= next_wait_progress:
                yield _sse_json_event(
                    "step",
                    indexed_step(
                        {
                            "name": "대기 중",
                            "detail": "처리 슬롯 대기",
                            "status": "in_progress",
                            "elapsed_ms": round(waited * 1000, 2),
                        }
                    ),
                )
                next_wait_progress += QUEUE_PROGRESS_INTERVAL_S
            continue
        event_type = event.get("type")
        if event_type == "busy":
            yield from _sse_busy_events()
            return
        if event_type == "step":
            yield _sse_json_event("step", event.get("item", {}))
            continue
        if event_type == "file_ready":
            yield from _sse_initial_text_events(
                conversation_id=event.get("conversation_id"),
                sources=tuple(event.get("sources", ())),
                text=str(event.get("text") or ""),
            )
            emitted = event.get("emitted")
            if isinstance(emitted, threading.Event):
                emitted.set()
            continue
        if event_type == "result":
            streamed_prefix = str(event.get("streamed_prefix") or "")
            if streamed_prefix:
                yield from _sse_events_from_final_answer(
                    event["final_answer"],
                    streamed_prefix=streamed_prefix,
                )
            else:
                yield from _sse_events_from_final_answer(event["final_answer"])
            return
        if event_type == "error":
            yield _sse_json_event(
                "error",
                {
                    "type": str(event.get("error_type") or "RuntimeError"),
                    "message": str(event.get("message") or "chat stream failed"),
                },
            )
            yield "event: done\ndata: error\n\n"
            return


@lru_cache(maxsize=1)
def _frontend_root() -> Path | None:
    candidates = (
        Path(__file__).resolve().parents[2] / "frontend",
        Path.cwd() / "frontend",
        Path("/app/frontend"),
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def _normalize_frontend_path(frontend_path: str) -> str:
    stripped = frontend_path.strip("/")
    if stripped in {"", "jw-chat-agent"}:
        return "index.html"
    prefix = "jw-chat-agent/"
    if stripped.startswith(prefix):
        return stripped.removeprefix(prefix)
    return stripped


def _frontend_file_response(frontend_path: str) -> FileResponse:
    root = _frontend_root()
    if root is None:
        raise HTTPException(status_code=404, detail="frontend not packaged")
    target = (root / frontend_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="frontend asset not found") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="frontend asset not found")
    return FileResponse(target)


def _sse_events(
    question: str,
    result: dict,
    conversation_id: str | None = None,
    *,
    history_store: ConversationHistoryStore | None = None,
    session_id: str | None = None,
    projection_context: ProjectionRequestContext | None = None,
    limiter: ChatConcurrencyLimiter | None = None,
    query_spec: RequestQuerySpec | None = None,
    shadow_request_id: str = "",
):
    if limiter is not None and not limiter.try_acquire():
        yield from _sse_busy_events()
        return
    streamed_prefix = _stream_ready_prefix(result)
    if streamed_prefix:
        yield from _sse_initial_text_events(
            conversation_id=conversation_id,
            sources=tuple(result.get("sources", ())),
            text=streamed_prefix,
        )
    try:
        with shadow_request_id_scope(shadow_request_id):
            final_answer = _compute_final_answer_with_query_spec(
                question,
                result,
                conversation_id,
                query_spec,
            )
        _record_conversation_history(
            history_store,
            session_id=session_id,
            question=question,
            final_answer=final_answer,
            projection_context=projection_context,
        )
    finally:
        if limiter is not None:
            limiter.release()
    if streamed_prefix:
        yield from _sse_events_from_final_answer(
            final_answer,
            streamed_prefix=streamed_prefix,
        )
    else:
        yield from _sse_events_from_final_answer(final_answer)


def _sse_busy_events():
    yield from selected_sse_presenter().busy_events(BUSY_MESSAGE)


def _file_ready_prefix(result: dict[str, Any]) -> str:
    if not result.get("file_only_ready"):
        return ""
    return cleanup_markdown_answer(str(result.get("answer") or ""))


_VERIFIED_EVIDENCE_PROGRESS_RE = re.compile(
    r"^(?:(?:임상시험|허가|안전성|환자수|최신 자료)\s+\d+건)"
    r"(?:\s*·\s*(?:임상시험|허가|안전성|환자수|최신 자료)\s+\d+건)*"
    r"\s*의 근거를 확인했습니다\.\s*"
    r"확인된 자료를 종합해 답변을 정리하고 있어요\.\s*"
)


def _stream_ready_prefix(result: dict[str, Any]) -> str:
    return _file_ready_prefix(result)


def _strip_verified_evidence_progress(answer: str) -> str:
    return _VERIFIED_EVIDENCE_PROGRESS_RE.sub("", answer, count=1)


def _sse_initial_text_events(
    *,
    conversation_id: str | None,
    sources: tuple[str, ...],
    text: str,
):
    yield from selected_sse_presenter().initial_text_events(
        conversation_id=conversation_id,
        source_labels=source_labels(sources),
        text=text,
    )


def _sse_events_from_final_answer(
    final_answer: FinalAnswer,
    *,
    streamed_prefix: str = "",
):
    yield from selected_sse_presenter().final_answer_events(
        conversation_id=final_answer.conversation_id,
        source_labels=source_labels(final_answer.sources),
        file_sources=_project_public_file_sources(final_answer.file_sources),
        text=final_answer.text,
        charts=final_answer.charts,
        timing=final_answer.timing,
        trace=final_answer.trace,
        streamed_prefix=streamed_prefix,
    )


def _record_conversation_history(
    history_store: ConversationHistoryStore | None,
    *,
    session_id: str | None,
    question: str,
    final_answer: FinalAnswer,
    projection_context: ProjectionRequestContext | None = None,
) -> None:
    if history_store is None:
        return
    try:
        history_store.record_turn(
            session_id=session_id,
            conversation_id=final_answer.conversation_id,
            question_text=question,
            answer_text=final_answer.text,
            trace=final_answer.trace,
            timing=final_answer.timing,
            sources=final_answer.sources,
            charts=final_answer.charts,
            projection_context=projection_context,
            conversation_slots=final_answer.conversation_slots,
        )
    except Exception:
        LOGGER.exception("failed to persist chat conversation history")


def _file_source_items(result: dict) -> tuple[dict[str, Any], ...]:
    items = result.get("file_source_items")
    if not isinstance(items, list):
        return ()
    return _project_public_file_sources(item for item in items if isinstance(item, dict))


def _project_public_file_sources(items: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    public_keys = (
        "file_name",
        "i_page",
        "slide_number",
        "section_title",
        "source_channel",
        "sheet_name",
        "row_start",
        "row_end",
    )
    projected: list[dict[str, Any]] = []
    for item in items:
        public_item = {key: item[key] for key in public_keys if item.get(key) is not None}
        if "file_name" in public_item:
            public_item["file_name"] = scrub_internal_terminology(str(public_item["file_name"]))
        projected.append(public_item)
    return tuple(projected)


def _run_legacy_answer_stages(answer: str, stages: Iterable[Any]) -> str:
    for pipeline_stage in stages:
        answer = pipeline_stage.transform(answer)
    return answer


def compute_final_answer(
    question: str,
    result: dict,
    conversation_id: str | None = None,
    *,
    query_spec: RequestQuerySpec | None = None,
) -> FinalAnswer:
    query_spec = query_spec or current_query_spec()
    fingerprint = question_fingerprint(question)
    try:
        final_answer = replace(
            _compute_final_answer(question, result, conversation_id),
            conversation_slots=extract_conversation_slots(result),
        )
        notice = cleanup_markdown_answer(str(result.get("conversation_interpretation") or ""))
        typed_failure = normalize_typed_failure(result)
        if typed_failure is None:
            surface_result = apply_final_surface_assembly(
                question,
                final_answer.text,
                query_spec,
                markdown_response=(
                    result.get("markdown_response")
                    if isinstance(result.get("markdown_response"), Mapping)
                    else None
                ),
            )
            answer = surface_result.answer
        else:
            answer = final_answer.text
        if notice and not answer.startswith(notice):
            answer = f"{notice}\n\n{answer}" if answer else notice
        raw_calls = result.get("tool_calls")
        tool_calls = (
            tuple(call for call in raw_calls if isinstance(call, Mapping))
            if isinstance(raw_calls, list)
            else ()
        )
        if query_spec is not None:
            try:
                observe_actual_coverage(
                    query_spec,
                    tool_calls,
                    question_fingerprint=fingerprint,
                )
            except Exception:  # noqa: BLE001 - shadow observation cannot alter answer delivery
                emit_shadow_gate_exception(
                    gate=ShadowGate.OPERATION_CONTRACT,
                    phase="actual",
                    question_fingerprint=fingerprint,
                    entity_count=len(query_spec.entities),
                    metric_count=len(query_spec.metrics),
                )
                LOGGER.exception("operation_contract_actual_shadow_failed")
        format_result = apply_response_format_contract(
            question,
            answer,
            tool_calls=tool_calls,
            sources=final_answer.sources,
        )
        output_policy_decision = evaluate_output_leakage(format_result.answer)
        user_answer = enforced_answer(format_result.answer, output_policy_decision)
        if query_spec is not None:
            try:
                observe_surface_coverage(
                    query_spec,
                    user_answer,
                    tool_calls,
                    question_fingerprint=fingerprint,
                    baseline_answer=user_answer,
                    served_answer=user_answer,
                )
            except Exception:  # noqa: BLE001 - shadow observation cannot alter answer delivery
                emit_shadow_gate_exception(
                    gate=ShadowGate.OPERATION_CONTRACT,
                    phase="surface",
                    question_fingerprint=fingerprint,
                    entity_count=len(query_spec.entities),
                    metric_count=len(query_spec.metrics),
                )
                LOGGER.exception("operation_contract_surface_shadow_failed")
        try:
            observe_typed_failure(
                result,
                legacy_answer=user_answer,
                served_answer=user_answer,
                question_fingerprint=fingerprint,
            )
        except Exception:  # noqa: BLE001 - shadow observation cannot alter answer delivery
            emit_shadow_gate_exception(
                gate=ShadowGate.TYPED_FAILURE_MODEL,
                phase="surface",
                question_fingerprint=fingerprint,
            )
            LOGGER.exception("typed_failure_model_shadow_failed")
        trace_result = {
            **result,
            "_response_format_contract": format_result.report.to_dict(),
            "_sec12_output_leakage_decision": output_policy_decision,
        }
        trace = trace_envelope(
            question=question,
            result=trace_result,
            answer=user_answer,
            charts=final_answer.charts,
            timing=final_answer.timing,
            conversation_id=conversation_id,
        )
        return replace(
            final_answer,
            text=user_answer,
            trace=trace,
        )
    finally:
        clear_current_query_spec()


def _compute_final_answer_with_query_spec(
    question: str,
    result: dict,
    conversation_id: str | None,
    query_spec: RequestQuerySpec | None,
) -> FinalAnswer:
    if query_spec is None:
        return compute_final_answer(question, result, conversation_id)
    return compute_final_answer(
        question,
        result,
        conversation_id,
        query_spec=query_spec,
    )


def _compute_final_answer(question: str, result: dict, conversation_id: str | None = None) -> FinalAnswer:
    if result.get("context_scope") == ContextScope.MIXED.value:
        return _compute_mixed_final_answer(question, result, conversation_id)
    deep_mode = result.get("research_mode") == "deep"
    active_question = str(result.get("effective_question") or question)
    enriched_markdown_response = positioning_markdown_response(
        active_question,
        result.get("markdown_response"),
        result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else [],
    )
    if enriched_markdown_response is not result.get("markdown_response"):
        result = {**result, "markdown_response": enriched_markdown_response}
    timing = ensure_timing(result)
    if result.get("conversation_fallback_ready"):
        record_answer_delivery(
            result,
            answer_branch="conversation_fallback",
            source_notice_attached=False,
        )
        timing_payload = finish(timing)
        answer = scrub_internal_terminology(cleanup_markdown_answer(str(result.get("answer") or "")))
        trace = trace_envelope(
            question=question,
            result=result,
            answer=answer,
            charts=[],
            timing=timing_payload,
            conversation_id=conversation_id,
        )
        return FinalAnswer(
            text=answer,
            charts=[],
            timing=timing_payload,
            trace=trace,
            sources=(),
            conversation_id=conversation_id,
        )
    client = GenosClient.for_deep_research() if deep_mode else GenosClient()
    if result.get("general_view_ready") and not deep_mode:
        record_answer_delivery(
            result,
            answer_branch="general_view_ready",
            source_notice_attached=False,
        )
        timing_payload = finish(timing)
        markdown_response = result.get("markdown_response")
        answer = replace_internal_fact_dump(
            question,
            cleanup_markdown_answer(str(result.get("answer") or "")),
            markdown_response,
        )
        answer = enforce_answer_contract(
            question,
            answer,
            markdown_response,
            result.get("general_view_contract"),
            tool_calls=tuple(result.get("tool_calls") or ()),
        )
        answer = _apply_relational_claim_gate(
            active_question,
            answer,
            result,
        )
        answer = enforce_market_answer_contract(
            question,
            answer,
            result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else (),
        )
        answer = _apply_relational_claim_gate(
            active_question,
            answer,
            result,
        )
        answer = _apply_evidence_binding_gate(active_question, answer, result)
        answer, source_notice_attached = append_source_basis_notice(answer, markdown_response)
        record_source_notice_attachment(
            result,
            attached=source_notice_attached,
        )
        trace = trace_envelope(
            question=question,
            result=result,
            answer=answer,
            charts=[],
            timing=timing_payload,
            conversation_id=conversation_id,
        )
        return FinalAnswer(
            text=answer,
            charts=[],
            timing=timing_payload,
            trace=trace,
            sources=tuple(result.get("sources", ())),
            conversation_id=conversation_id,
        )
    if result.get("file_only_ready"):
        record_answer_delivery(
            result,
            answer_branch="file_only",
            source_notice_attached=False,
        )
        answer = cleanup_markdown_answer(str(result.get("answer") or ""))
        overviews = deserialize_file_overviews(result.get("file_brief_observed"))
        if overviews:
            try:
                generated = client.uploaded_file_brief(build_file_brief_messages(overviews))
                brief = parse_and_render_file_briefs(generated, overviews)
            except (requests.RequestException, FileBriefValidationError):
                brief = ""
            if brief:
                answer = cleanup_markdown_answer(f"{answer}\n\n{brief}")
        timing_payload = finish(timing)
        trace = trace_envelope(
            question=question,
            result=result,
            answer=answer,
            charts=[],
            timing=timing_payload,
            conversation_id=conversation_id,
        )
        return FinalAnswer(
            text=answer,
            charts=[],
            timing=timing_payload,
            trace=trace,
            sources=tuple(result.get("sources", ())),
            conversation_id=conversation_id,
        )
    partial_evidence_result = _is_partial_evidence_result(result)
    if (
        _is_market_clarification_result(result)
        or _is_market_membership_mismatch_result(result)
        or _is_terminal_typed_result(result)
        or partial_evidence_result
    ):
        record_answer_delivery(
            result,
            answer_branch="typed_partial" if partial_evidence_result else "typed_terminal",
            source_notice_attached=False,
        )
        timing_payload = finish(timing)
        answer = scrub_internal_terminology(cleanup_markdown_answer(str(result.get("answer") or "")))
        trace = trace_envelope(
            question=question,
            result=result,
            answer=answer,
            charts=[],
            timing=timing_payload,
            conversation_id=conversation_id,
        )
        return FinalAnswer(
            text=answer,
            charts=[],
            timing=timing_payload,
            trace=trace,
            sources=tuple(result.get("sources", ())),
            conversation_id=conversation_id,
        )
    file_context_fact = _file_context_fact(result)
    market_contract_allowed = result.get("context_scope") != ContextScope.FILE.value
    deterministic_file_answer = str(result.get("deterministic_file_answer") or "").strip()
    deterministic_market_answer = (
        _deterministic_simple_market_answer(active_question, result)
        if not deep_mode and market_contract_allowed and not file_context_fact
        else ""
    )
    if deterministic_market_answer:
        record_answer_delivery(
            result,
            answer_branch="app_deterministic_market",
            source_notice_attached=False,
        )
        generated_answer = deterministic_market_answer
    elif deterministic_file_answer and not _requires_cross_file_synthesis(active_question, result):
        record_answer_delivery(
            result,
            answer_branch="app_deterministic_file",
            source_notice_attached=False,
        )
        generated_answer = deterministic_file_answer
    else:
        try:
            generation_stage = "deep_research_synthesis" if deep_mode else "answer_generation_total"
            generation_detail = "gemini-3.1-pro 근거 종합" if deep_mode else "GenOS expression plus safety"
            with stage(timing, generation_stage, generation_detail):
                generated_answer = "".join(client.stream_answer(active_question, result))
            if getattr(client, "answer_branch_events", ()):
                record_answer_delivery(
                    result,
                    answer_branch=client.answer_branch_events[-1],
                    source_notice_attached=False,
                )
        except requests.RequestException:
            record_answer_delivery(
                result,
                answer_branch="app_generation_request_fallback",
                source_notice_attached=False,
            )
            generated_answer = finalized_fallback_fact_answer(active_question, result.get("markdown_response"))
    for call in client.token_usage_calls:
        record_token_usage(timing, call)
    with stage(timing, "answer_cleanup", "markdown cleanup"):
        safe_answer = cleanup_markdown_answer(generated_answer)
        markdown_response = result.get("markdown_response")
        fact_md = ""
        if isinstance(markdown_response, dict):
            fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
            safe_answer = ensure_top_brand_trend_table(safe_answer, fact_md)
        policy_fact_md = "\n\n".join(part for part in (fact_md, file_context_fact) if part)
        safe_answer = apply_claim_policy(active_question, safe_answer, policy_fact_md)
    chart_after_binding = _chart_after_evidence_binding_enabled()
    charts: list[dict[str, Any]] = []
    if not chart_after_binding:
        try:
            with stage(timing, "chart_generation", "fact-backed chart spec"):
                charts = build_charts(result, question=active_question, answer=safe_answer)
        except Exception:
            charts = []
        timing_payload = finish(timing)
    router_diagnostics = result.get("router_diagnostics")
    external_tool_agent_result = (
        isinstance(router_diagnostics, dict)
        and router_diagnostics.get("mode") == "tool_use_agent"
    )
    structural_contract = str(
        evaluate_answer_contract(active_question, "", None).get("structural_contract") or ""
    )
    general_contracts_allowed = not deep_mode and (
        not external_tool_agent_result or structural_contract == "positioning"
    )

    pre_chart_stages, post_chart_stages = build_answer_pipeline_stages(
        AnswerPipelineContext(
            question=active_question,
            result=result,
            markdown_response=markdown_response,
            fact_md=fact_md,
            policy_fact_md=policy_fact_md,
            file_context_fact=file_context_fact,
            deep_mode=deep_mode,
            market_contract_allowed=market_contract_allowed,
            general_contracts_allowed=general_contracts_allowed,
            external_tool_agent_result=external_tool_agent_result,
            empty_file_answer=_looks_like_empty_file_context_answer,
            file_context_fallback=_file_context_fallback_answer,
            append_file_context_source=_append_file_context_source,
            record_source_notice=lambda attached: record_source_notice_attachment(
                result,
                attached=attached,
            ),
            relational_claim_gate=lambda answer: _apply_relational_claim_gate(
                active_question,
                answer,
                result,
            ),
            natural_fact_lead=lambda answer: ensure_natural_fact_lead(
                active_question,
                answer,
                fact_md,
            ),
            file_postprocess_isolation=lambda answer: _enforce_file_postprocess_isolation(answer, result),
            evidence_binding_gate=lambda answer: _apply_evidence_binding_gate(active_question, answer, result),
            strip_verified_progress=_strip_verified_evidence_progress,
        )
    )
    safe_answer = run_selected_answer_pipeline(
        safe_answer,
        pre_chart_stages,
        legacy=lambda answer: _run_legacy_answer_stages(answer, pre_chart_stages),
    )
    if chart_after_binding:
        try:
            with stage(timing, "chart_generation", "bound fact-backed chart spec"):
                candidate_charts = build_charts(
                    result,
                    question=active_question,
                    answer=safe_answer,
                )
                charts = filter_charts_for_binding(
                    candidate_charts,
                    result=result,
                    question=active_question,
                )
        except Exception:
            LOGGER.exception("bound_chart_generation_failed")
            charts = []
        timing_payload = finish(timing)
    # After the binding gate, so a refusal the orchestrator already decided on is
    # never weighed as an unbacked claim, and after every contract enforcer, so
    # none of them can drop it again.
    safe_answer = run_selected_answer_pipeline(
        safe_answer,
        post_chart_stages,
        legacy=lambda answer: _run_legacy_answer_stages(answer, post_chart_stages),
    )
    trace = trace_envelope(
        question=question,
        result=result,
        answer=safe_answer,
        charts=charts,
        timing=timing_payload,
        conversation_id=conversation_id,
    )
    return FinalAnswer(
        text=safe_answer,
        charts=charts,
        timing=timing_payload,
        trace=trace,
        sources=tuple(result.get("sources", ())),
        conversation_id=conversation_id,
        file_sources=_file_source_items(result),
    )


def _apply_relational_claim_gate(question: str, answer: str, result: dict[str, Any]) -> str:
    gate = enforce_relational_numeric_claims_with_trace(
        question,
        answer,
        result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else (),
    )
    previous = result.get("_qa_claim_gate")
    previous_items = previous if isinstance(previous, dict) else {}
    previous_reasons = previous_items.get("blocked_reasons")
    combined_reasons = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                *(previous_reasons if isinstance(previous_reasons, (list, tuple)) else ()),
                *gate.blocked_reasons,
            )
            if str(item)
        )
    )
    disposition_priority = {
        "answered": 0,
        "partial": 1,
        "cached_partial": 2,
        "unavailable": 3,
    }
    previous_disposition = str(previous_items.get("disposition") or "")
    disposition = max(
        (previous_disposition, gate.disposition),
        key=lambda item: disposition_priority.get(item, -1),
    )
    result["_qa_claim_gate"] = {
        "blocked_claim_count": int(previous_items.get("blocked_claim_count") or 0) + gate.blocked_claim_count,
        "blocked_reasons": combined_reasons,
        "disposition": disposition,
    }
    failure_kind = gate.failure_kind or str(previous_items.get("failure_kind") or "") or None
    if failure_kind:
        result["_qa_claim_gate"]["failure_kind"] = failure_kind
    return gate.answer


def _apply_evidence_binding_gate(question: str, answer: str, result: dict[str, Any]) -> str:
    facts = evidence_facts_from_result(result)
    try:
        observation = evidence_bundle_shadow_observation(facts)
    except Exception:  # noqa: BLE001 - shadow contract creation must remain fail-open
        LOGGER.exception("evidence_bundle_shadow_observation_failed")
    else:
        LOGGER.info("evidence_bundle_shadow_observed observation=%s", observation)
    expected_entities = expected_entities_from_result(question, result)
    expected_market_ids = expected_market_ids_from_result(result)
    binding_question = hira_binding_question(question)
    gate = verify_claim_bindings(
        question=binding_question,
        answer=answer,
        facts=facts,
        expected_entities=expected_entities,
        expected_market_ids=expected_market_ids,
    )
    observability = binding_pipeline_observability(
        question=binding_question,
        answer=answer,
        facts=facts,
        expected_entities=expected_entities,
        expected_market_ids=expected_market_ids,
        gate=gate,
        fact_input=evidence_fact_input_inventory(result, facts),
    )
    context_observability = binding_context_observability(
        question=binding_question,
        answer=answer,
        expected_entities=expected_entities,
        gate=gate,
        context_projection_allowed=not bool(
            str(result.get("file_context") or "").strip()
            or result.get("file_source_items")
            or str(result.get("context_scope") or "").strip().upper()
            in {"FILE", "MIXED"}
        ),
    )
    previous = result.get("_qa_claim_gate")
    previous_items = previous if isinstance(previous, dict) else {}
    previous_reasons = previous_items.get("blocked_reasons")
    combined_reasons = tuple(
        dict.fromkeys(
            str(item)
            for item in (
                *(previous_reasons if isinstance(previous_reasons, (list, tuple)) else ()),
                *gate.blocked_reasons,
            )
            if str(item)
        )
    )
    disposition_priority = {
        "answered": 0,
        "partial": 1,
        "cached_partial": 2,
        "unavailable": 3,
    }
    previous_disposition = str(previous_items.get("disposition") or "")
    disposition = max(
        (previous_disposition, gate.disposition),
        key=lambda item: disposition_priority.get(item, -1),
    )
    result["_qa_claim_gate"] = {
        "blocked_claim_count": int(previous_items.get("blocked_claim_count") or 0)
        + gate.blocked_claim_count,
        "blocked_reasons": combined_reasons,
        "disposition": disposition,
        "binding_status": gate.status,
        "blocked_numbers": gate.blocked_numbers,
    }
    if gate.rejections:
        result["_qa_claim_gate"]["rejections"] = tuple(
            item.to_trace() for item in gate.rejections
        )
    # Which return site produced the verdict, and how the token loop scored.
    # Always set: a reader must be able to tell "no substitution happened"
    # from "this key was never written". Counts stay None when the gate
    # returned before the token loop ran.
    result["_qa_claim_gate"]["binding_decision"] = {
        "decision_site": gate.decision_site,
        "substitution_triggered": bool(gate.substitution_triggered),
        "bind_attempted_count": gate.bind_attempted_count,
        "bind_succeeded_count": gate.bind_succeeded_count,
        "blocked_reason_histogram": (
            [[str(reason), int(count)] for reason, count in gate.blocked_reason_histogram]
            if gate.blocked_reason_histogram is not None
            else None
        ),
    }
    if observability:
        result["_qa_claim_gate"]["pipeline_observability"] = observability
    result["_qa_claim_gate"].update(context_observability)
    failure_kind = gate.failure_kind or str(previous_items.get("failure_kind") or "") or None
    if failure_kind:
        result["_qa_claim_gate"]["failure_kind"] = failure_kind
    return gate.answer


def _requires_cross_file_synthesis(question: str, result: dict) -> bool:
    if not re.search(r"(?:비교|대조|교차|일치|차이)", question, re.IGNORECASE):
        return False
    file_names = {
        str(item.get("file_name") or "").strip().casefold()
        for item in _file_source_items(result)
        if str(item.get("file_name") or "").strip()
    }
    return len(file_names) >= 2


def _compute_mixed_final_answer(
    question: str,
    result: dict,
    conversation_id: str | None,
) -> FinalAnswer:
    record_answer_delivery(
        result,
        answer_branch="mixed",
        source_notice_attached=False,
    )
    market_result = dict(result.get("mixed_market_result") or {})
    file_result = dict(result.get("mixed_file_result") or {})
    market_result["context_scope"] = ContextScope.MARKET.value
    market_markdown = market_result.get("markdown_response")
    if isinstance(market_markdown, dict):
        market_result["markdown_response"] = {
            **market_markdown,
            "context_scope": ContextScope.MARKET.value,
        }
    file_result["context_scope"] = ContextScope.FILE.value

    timeout_s = max(1.0, float(os.getenv("JW_CHAT_MIXED_LEG_TIMEOUT_S", "90")))
    total_timeout_s = max(1.0, float(os.getenv("JW_CHAT_MIXED_TOTAL_TIMEOUT_S", "95")))
    started = time.perf_counter()
    deadline_value = result.get("mixed_deadline_monotonic")
    deadline = (
        float(deadline_value)
        if isinstance(deadline_value, (int, float))
        else started + total_timeout_s
    )
    market_question = str(result.get("mixed_market_question") or question)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mixed-m1-final")
    market_future = executor.submit(
        _finalize_mixed_leg,
        "market",
        market_question,
        market_result,
        conversation_id,
    )
    file_future = executor.submit(
        _finalize_mixed_leg,
        "file",
        question,
        file_result,
        conversation_id,
    )
    try:
        try:
            market_final = market_future.result(timeout=min(timeout_s, _remaining_seconds(deadline)))
        except FutureTimeoutError:
            market_future.cancel()
            market_final = _mixed_leg_failure_answer("시장 데이터 조회가 처리 시간을 초과했습니다.", conversation_id)
        except Exception as exc:
            LOGGER.warning("mixed market finalization failed type=%s", type(exc).__name__)
            market_final = _mixed_leg_failure_answer(
                "시장 데이터 조회를 완료하지 못했습니다. 조회 오류입니다.",
                conversation_id,
            )
        try:
            file_final = file_future.result(timeout=min(timeout_s, _remaining_seconds(deadline)))
        except FutureTimeoutError:
            file_future.cancel()
            file_final = _mixed_leg_failure_answer("첨부 문서 조회가 처리 시간을 초과했습니다.", conversation_id)
        except Exception as exc:
            LOGGER.warning("mixed file finalization failed type=%s", type(exc).__name__)
            file_final = _mixed_leg_failure_answer(
                "첨부 문서 조회를 완료하지 못했습니다. 조회 오류입니다.",
                conversation_id,
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    file_name = _mixed_file_name(file_result)
    answer = cleanup_markdown_answer(
        "## 시장 데이터\n\n"
        f"{market_final.text.strip()}\n\n"
        f"## 첨부 문서 — {file_name}\n\n"
        f"{file_final.text.strip()}\n\n"
        "⚠️ 두 근거는 기준기간·단위·정의가 다를 수 있어 직접 비교하지 않습니다."
    )
    answer = scrub_internal_terminology(answer)
    timing = ensure_timing(result)
    timing_payload = finish(timing)
    timing_payload["mixed_legs"] = {
        "market": market_final.timing,
        "file": file_final.timing,
        "timeout_s": timeout_s,
        "total_timeout_s": total_timeout_s,
        "synthesis_llm_calls": 0,
    }
    trace = trace_envelope(
        question=question,
        result=result,
        answer=answer,
        charts=[*market_final.charts, *file_final.charts],
        timing=timing_payload,
        conversation_id=conversation_id,
    )
    return FinalAnswer(
        text=answer,
        charts=[*market_final.charts, *file_final.charts],
        timing=timing_payload,
        trace=trace,
        sources=tuple(dict.fromkeys([*market_final.sources, *file_final.sources])),
        conversation_id=conversation_id,
        file_sources=file_final.file_sources,
    )


def _deterministic_simple_market_answer(question: str, result: dict) -> str:
    normalized = re.sub(r"\s+", " ", question).strip()
    decomposition = result.get("decomposition")
    market_members = isinstance(decomposition, list) and any(
        isinstance(item, dict) and item.get("intent") == "market_members"
        for item in decomposition
    )
    if market_members:
        calls = result.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            return ""
        if any(not isinstance(call, dict) or call.get("tool") != "get_market_members" for call in calls):
            return ""
        markdown_response = result.get("markdown_response")
        deterministic_markdown = (
            str(markdown_response.get("markdown") or "")
            if isinstance(markdown_response, dict)
            else ""
        )
        contracted = (deterministic_markdown or str(result.get("answer") or "")).strip()
        if not re.search(r"총\s*[0-9,]+개\s*중\s*[0-9,]+개\s*표시", contracted):
            return ""
        return contracted
    same_market_sales = isinstance(decomposition, list) and any(
        isinstance(item, dict) and item.get("intent") == "same_market_sales"
        for item in decomposition
    )
    if not same_market_sales and not re.search(r"시장\s*규모", normalized):
        return ""
    if any(
        token in normalized
        for token in ("추이", "변화", "증감", "왜", "원인", "전망", "비교", "경쟁", "채널", "분석")
    ):
        return ""
    calls = result.get("tool_calls")
    if not isinstance(calls, list):
        return ""
    if same_market_sales:
        markdown_response = result.get("markdown_response")
        deterministic_markdown = (
            str(markdown_response.get("markdown") or "")
            if isinstance(markdown_response, dict)
            else ""
        )
        contracted = deterministic_markdown or render_same_market_sales_answer(calls)
    else:
        contracted = enforce_market_answer_contract(normalized, "", calls)
    contracted = contracted.strip()
    if "시장규모" not in contracted and "지원되지 않는 시장 매핑" not in contracted:
        return ""
    return contracted


def _finalize_mixed_leg(
    leg: str,
    question: str,
    result: dict,
    conversation_id: str | None,
) -> FinalAnswer:
    error = str(result.get("mixed_leg_error") or "").strip()
    if error:
        return _mixed_leg_failure_answer(error, conversation_id)
    if not result:
        message = "첨부 문서 근거가 없습니다." if leg == "file" else "시장 데이터 근거가 없습니다."
        return _mixed_leg_failure_answer(message, conversation_id)
    if leg == "file":
        return _deterministic_mixed_file_answer(result, conversation_id)
    return compute_final_answer(question, result, conversation_id)


_MIXED_FILE_METADATA_RE = re.compile(
    r"^(?:\[\d+\]\s|\[DA\]\s|검색 범위:)|document_id=|TEMP_DOCUMENT_",
    re.IGNORECASE,
)
_MIXED_FILE_PAGE_RE = re.compile(r"\bp\.(\d+)\b", re.IGNORECASE)


def _deterministic_mixed_file_answer(
    result: dict,
    conversation_id: str | None,
) -> FinalAnswer:
    context = str(result.get("file_context") or "").strip()
    deterministic = str(result.get("deterministic_file_answer") or "").strip()
    evidence = deterministic or _public_mixed_file_evidence(context)
    if not evidence:
        return _mixed_leg_failure_answer("첨부 문서 근거를 가져오지 못했습니다.", conversation_id)

    file_name = _mixed_file_name(result)
    page = _mixed_file_page(result, context)
    source = f"출처: 업로드 문서 · {file_name}"
    if page is not None:
        source += f" · p.{page}"
    answer = scrub_internal_terminology(cleanup_markdown_answer(f"{evidence}\n\n{source}"))
    return FinalAnswer(
        text=answer,
        charts=[],
        timing={"deterministic_file_render": True, "synthesis_llm_calls": 0},
        trace={},
        sources=("document",),
        conversation_id=conversation_id,
        file_sources=_file_source_items(result),
    )


def _public_mixed_file_evidence(context: str) -> str:
    if not context:
        return ""
    kept = [
        line.strip()
        for line in context.splitlines()
        if line.strip() and not _MIXED_FILE_METADATA_RE.search(line.strip())
    ]
    max_chars = max(200, int(os.getenv("JW_CHAT_MIXED_FILE_EVIDENCE_MAX_CHARS", "6000")))
    return "\n\n".join(kept)[:max_chars].strip()


def _mixed_file_page(result: dict, context: str) -> int | None:
    for item in _file_source_items(result):
        value = item.get("i_page")
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
    match = _MIXED_FILE_PAGE_RE.search(context)
    return int(match.group(1)) if match else None


def _mixed_leg_failure_answer(message: str, conversation_id: str | None) -> FinalAnswer:
    return FinalAnswer(
        text=message,
        charts=[],
        timing={},
        trace={},
        sources=(),
        conversation_id=conversation_id,
    )


def _mixed_file_name(result: dict) -> str:
    for item in _file_source_items(result):
        name = str(item.get("file_name") or "").strip()
        if name:
            return name
    return "업로드 문서"


def _is_market_clarification_result(result: dict) -> bool:
    decomposition = result.get("decomposition")
    if not isinstance(decomposition, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("intent") in {"market_clarification", "brand_cardinality_clarification"}
        and item.get("status") == "needs_clarification"
        for item in decomposition
    )


def _is_market_membership_mismatch_result(result: dict) -> bool:
    decomposition = result.get("decomposition")
    if not isinstance(decomposition, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("intent") == "market_membership_validation"
        and item.get("status") == "unsupported"
        for item in decomposition
    )


def _is_terminal_typed_result(result: dict) -> bool:
    typed_failure = normalize_typed_failure(result)
    if (
        typed_failure is not None
        and typed_failure.code is TypedFailureCode.DISEASE_CODE_ABSENT
    ):
        return True

    sources = result.get("sources")
    if isinstance(sources, (list, tuple)) and len(sources) == 1:
        if str(sources[0] or "") in {
            "unsupported_brand",
            "ambiguous_brand",
            "strategic_market_not_member",
            "unsupported_hira_interface",
            "field_not_exposed",
            "brand_unresolved",
        }:
            return True

    diagnostics = result.get("router_diagnostics")
    decomposition = result.get("decomposition")
    if (
        isinstance(diagnostics, dict)
        and diagnostics.get("gate") == "typed_unavailable"
        and isinstance(decomposition, list)
        and any(isinstance(item, dict) and item.get("status") == "no_data" for item in decomposition)
    ):
        return True
    if not isinstance(diagnostics, dict) or diagnostics.get("mode") != "tool_use_agent":
        return False
    routing_v4 = diagnostics.get("routing_v4")
    if isinstance(routing_v4, dict):
        official_web_fallback = routing_v4.get("official_web_fallback")
        if (
            isinstance(official_web_fallback, dict)
            and official_web_fallback.get("reason_code") == "IDENTITY_MISMATCH"
            and official_web_fallback.get("calls_executed") == 0
        ):
            return True
        if (
            isinstance(official_web_fallback, dict)
            and official_web_fallback.get("reason_code") == "UPSTREAM_UNAVAILABLE"
            and is_actionable_upstream_guidance(str(result.get("answer") or ""))
        ):
            return True
    return diagnostics.get("fallback_code") in {
        "UNSUPPORTED_QUERY",
        "VERIFICATION_FAIL",
    }


def _is_partial_evidence_result(result: dict) -> bool:
    typed_failure = normalize_typed_failure(result)
    return (
        typed_failure is not None
        and typed_failure.code is TypedFailureCode.PARTIAL_EVIDENCE
        and typed_failure.partial
        and not typed_failure.terminal
    )


def _sse_delta(token: str) -> str:
    return selected_sse_presenter().delta(token)


def _sse_json_event(event_name: str, payload: object) -> str:
    return selected_sse_presenter().json_event(event_name, payload)


app = create_app(startup_warmup=startup_warmup_from_env())
