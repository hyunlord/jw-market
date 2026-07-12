from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from hmac import compare_digest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from jw_chat_agent_poc.agent_loop import should_use_agent_loop
from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies, build_tool_use_agent, unsupported_brand_result
from jw_chat_agent_poc.portfolio_scope import is_portfolio_decline_question
from jw_chat_agent_poc.orchestrator import ChatAgent
from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract
from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_labels
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate, requested_unavailable_source
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.resolver import UnsupportedBrandError
from jw_chat_agent_poc.service.answer_safety import (
    append_deterministic_source_block,
    cleanup_markdown_answer,
    ensure_file_absence_statement,
    ensure_top_brand_trend_table,
    finalized_fallback_fact_answer,
)
from jw_chat_agent_poc.service.charts import build_charts
from jw_chat_agent_poc.service.concurrency import BUSY_MESSAGE, ChatBusyError, ChatConcurrencyLimiter
from jw_chat_agent_poc.service.conversation import ConversationStore, PendingClarification
from jw_chat_agent_poc.service.conversation_context import (
    extract_conversation_slots,
    resolve_anaphora,
    reused_context_result,
    unresolved_reference_result,
)
from jw_chat_agent_poc.service.conversation_history import ConversationHistoryStore, MySQLConversationHistoryStore
from jw_chat_agent_poc.service.context_scope import ContextScope, resolve_context_scope
from jw_chat_agent_poc.service.file_search_client import search_uploaded_files
from jw_chat_agent_poc.service.genos_client import GenosClient, append_blocked_metric_notices_from_markdown_response
from jw_chat_agent_poc.service.general_view_routing import GeneralRoute
from jw_chat_agent_poc.service.history_projection import (
    HistoryProjectionRuntime,
    ProjectionRequestContext,
    sanitize_http_headers,
    trusted_portal_user_id,
)
from jw_chat_agent_poc.service.models import ChatAccepted, ChatAnswer, ChatRequest, HealthResponse
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope, version_payload
from jw_chat_agent_poc.service.sse_protocol import iter_markdown_sse_events
from jw_chat_agent_poc.service.startup_warmup import (
    DisabledStartupWarmup,
    StartupWarmup,
    startup_warmup_from_env,
)
from jw_chat_agent_poc.common.timing import StageEventSink, ensure_timing, finish, stage, stage_event_sink
from jw_chat_agent_poc.common.token_usage import record_token_usage
from jw_chat_agent_poc.tools.external import resolve_patent_ingredient_query
from jw_chat_agent_poc.tools.metrics.market_scope import (
    MarketScopeResolver,
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
| file_sources | JSON array `[{file_name, document_id?}]` | after sources, only when uploaded-file grounding was used | 0-1 |
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


SESSION_STORE_MAX_ENV = "SESSION_STORE_MAX"
DEFAULT_SESSION_STORE_MAX = 500


class SessionStore:
    def __init__(self, conversations: ConversationStore | None = None, max_sessions: int | None = None) -> None:
        if max_sessions is None:
            max_sessions = int(os.environ.get(SESSION_STORE_MAX_ENV, str(DEFAULT_SESSION_STORE_MAX)))
        self._max_sessions = max(1, max_sessions)
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self.conversations = conversations or ConversationStore()

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
    warmup = startup_warmup or DisabledStartupWarmup()

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
                )
        except ChatBusyError as exc:
            raise HTTPException(status_code=503, detail=BUSY_MESSAGE) from exc
        session_id = store.put({"question": request.question, "result": result["result"]})
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
                )
                final_answer = compute_final_answer(item["question"], item["result"], item.get("conversation_id"))
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
            file_sources=[dict(item) for item in final_answer.file_sources],
        )

    @app.get(
        "/chat/stream",
        summary="Stream a chat answer as Server-Sent Events",
        description=CHAT_STREAM_DESCRIPTION,
    )
    def chat_stream(
        session_id: str | None = Query(default=None),
        question: str | None = Query(default=None),
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
) -> dict:
    sink_context = stage_event_sink(timing_sink) if timing_sink is not None else nullcontext()
    with sink_context:
        state = store.conversations.get_or_create(conversation_id)
        delegated_file_context, file_source_items, has_active_upload = _delegated_file_context(
            question, state.conversation_id, file_context
        )
        has_file = _has_file_signal(documents, delegated_file_context) or has_active_upload
        context_scope = resolve_context_scope(
            question,
            has_active_file=has_file,
            is_fresh_upload=bool(documents),
            has_market_intent=_has_market_intent(question),
        )
        if not question.strip() and _has_file_signal(documents, delegated_file_context):
            result = _file_only_ready_result(documents, delegated_file_context)
        elif context_scope is ContextScope.FILE:
            result = _file_scoped_result(question)
        else:
            result = _answer_with_conversation(
                store,
                market_scope_resolver,
                agent_factory,
                state.conversation_id,
                question,
                external_mode,
                [],
                use_direct_agent_loop=use_direct_agent_loop,
            )
        result = _attach_file_context(result, delegated_file_context, file_source_items)
        result = _annotate_context_scope(result, context_scope)
        store.conversations.record_exchange(
            state.conversation_id,
            question,
            str(result.get("answer") or ""),
            _applied_filters(result),
            slots=extract_conversation_slots(result),
        )
        return {"question": question, "result": result, "conversation_id": state.conversation_id}


def _delegated_file_context(
    question: str, conversation_id: str | None, file_context: str | None
) -> tuple[str | None, tuple[dict[str, Any], ...], bool]:
    contexts: list[str] = []
    file_source_items: tuple[dict[str, Any], ...] = ()
    uploaded = search_uploaded_files(question, conversation_id)
    has_active_upload = bool(uploaded and uploaded.has_active_file)
    if uploaded is not None and uploaded.file_context.strip():
        contexts.append(uploaded.file_context.strip())
        file_source_items = uploaded.file_source_items
    provided = (file_context or "").strip()
    if provided:
        contexts.append(provided)
    if not contexts:
        return None, (), has_active_upload
    return "\n\n".join(dict.fromkeys(contexts)), file_source_items, has_active_upload


def _has_file_signal(documents: list[Path] | None, file_context: str | None) -> bool:
    return bool(documents) or bool((file_context or "").strip())


def _has_market_intent(question: str) -> bool:
    return detect_market_scope_intent(question) is not None or should_use_agent_loop(question)


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


def _annotate_context_scope(result: dict, scope: ContextScope) -> dict:
    copied = dict(result)
    copied["context_scope"] = scope.value
    markdown = copied.get("markdown_response")
    if isinstance(markdown, dict):
        copied["markdown_response"] = {**markdown, "context_scope": scope.value}
    return copied


def _file_only_ready_result(documents: list[Path] | None, file_context: str | None) -> dict:
    file_names = [path.name for path in documents or []]
    count = len(file_names)
    count_text = f"{count}개" if count else ""
    subject = f"파일 {count_text}".strip()
    answer = f"{subject} 저장 완료했습니다. 이 세션에서 질문하면 업로드한 파일을 참조해 답변합니다."
    if file_names:
        answer = f"{answer}\n\n" + "\n".join(f"- {name}" for name in file_names)
    if file_context and not file_names:
        answer = f"{answer}\n\n- 업로드 파일"
    return {
        "answer": cleanup_markdown_answer(answer),
        "sources": ["file_upload"],
        "tool_calls": [],
        "file_only_ready": True,
        "file_names": file_names,
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
    resolution = resolve_anaphora(question, previous_turn)
    if resolution.unresolved_reference:
        return unresolved_reference_result(question)
    if resolution.reusable_ranked is not None:
        return reused_context_result(question, resolution.reusable_ranked, previous_turn.slots if previous_turn else None)
    return _answer_without_pending(
        market_scope_resolver,
        agent_factory,
        conversation_id,
        resolution.resolved_question,
        external_mode,
        documents,
        store,
        use_direct_agent_loop=use_direct_agent_loop,
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
) -> dict:
    if requested_unavailable_source(question) is not None and not documents:
        with stage(None, "question_classification", "agent setup"):
            agent = agent_factory(external_mode=external_mode)
        return agent.answer(question, documents)
    if use_direct_agent_loop and should_use_agent_loop(question) and not documents:
        return _answer_direct_agent_loop(question, external_mode)
    if should_use_agent_loop(question):
        with stage(None, "question_classification", "agent setup"):
            agent = agent_factory(external_mode=external_mode)
        return agent.answer(question, documents)
    intent = detect_market_scope_intent(question)
    if intent is not None:
        if intent.requires_clarification:
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
            return market_scope_resolver.clarification(question, brand=brand)
        if intent.view_type is not None:
            return market_scope_resolver.answer(question, view_type=intent.view_type)
    with stage(None, "question_classification", "agent setup"):
        agent = agent_factory(external_mode=external_mode)
    return agent.answer(question, documents)


def _answer_direct_agent_loop(question: str, external_mode: str) -> dict:
    with stage(None, "question_classification", "agent setup"):
        dependencies = build_chat_agent_dependencies(external_mode=external_mode)
    with stage(None, "question_decomposition", "BQ and tool routing"):
        routes = dependencies.router.route(question, has_documents=False)
    if not is_portfolio_decline_question(question, routes) and not _is_known_ingredient_patent_question(question):
        try:
            dependencies.resolver.resolve(question, allow_default=False)
        except UnsupportedBrandError:
            return unsupported_brand_result(question, routes, router_diagnostics(dependencies.router))
    return build_tool_use_agent(dependencies.agent_loop_dependencies()).answer(question)


def _is_known_ingredient_patent_question(question: str) -> bool:
    lower = question.lower()
    asks_patent = "특허" in question or "patent" in lower or "orange" in lower
    return asks_patent and resolve_patent_ingredient_query(question) is not None


def _prepend_pending_notice(result: dict) -> dict:
    copied = dict(result)
    notice = "이전 시장 기준 선택 요청은 이번 답변과 매칭되지 않아 새 질문으로 처리했습니다.\n\n"
    copied["answer"] = notice + str(result.get("answer") or "")
    return copied


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
        item = indexed_step(event)
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
            with stage_event_sink(emit_step):
                with stage(None, "answer_generation_total", "request processing"):
                    item = _answer_question(
                        store,
                        market_scope_resolver,
                        agent_factory,
                        question,
                        external_mode,
                        conversation_id,
                        use_direct_agent_loop=use_direct_agent_loop,
                        timing_sink=emit_step,
                    )
                    final_answer = compute_final_answer(item["question"], item["result"], item.get("conversation_id"))
            _record_conversation_history(
                history_store,
                session_id=None,
                question=item["question"],
                final_answer=final_answer,
                projection_context=projection_context,
            )
            events.put({"type": "result", "item": item, "final_answer": final_answer})
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
                yield _sse_json_event("step", indexed_step({"name": "대기 중", "detail": "처리 슬롯 대기", "status": "in_progress", "raw_name": "queue_wait", "raw_detail": "concurrency slot", "elapsed_ms": round(waited * 1000, 2)}))
                next_wait_progress += QUEUE_PROGRESS_INTERVAL_S
            continue
        event_type = event.get("type")
        if event_type == "busy":
            yield from _sse_busy_events()
            return
        if event_type == "step":
            yield _sse_json_event("step", event.get("item", {}))
            continue
        if event_type == "result":
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
):
    if limiter is not None and not limiter.try_acquire():
        yield from _sse_busy_events()
        return
    try:
        final_answer = compute_final_answer(question, result, conversation_id)
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
    yield from _sse_events_from_final_answer(final_answer)


def _sse_busy_events():
    yield _sse_delta(BUSY_MESSAGE)
    yield _sse_json_event("error", {"type": "ServiceBusy", "message": BUSY_MESSAGE})
    yield "event: done\ndata: error\n\n"


def _sse_events_from_final_answer(final_answer: FinalAnswer):
    if final_answer.conversation_id:
        yield f"event: conversation\ndata: {final_answer.conversation_id}\n\n"
    yield f"event: sources\ndata: {','.join(source_labels(final_answer.sources))}\n\n"
    if final_answer.file_sources:
        yield _sse_json_event("file_sources", list(final_answer.file_sources))
    yield from iter_markdown_sse_events(final_answer.text)
    if final_answer.charts:
        yield _sse_json_event("charts", final_answer.charts)
    yield _sse_json_event("timing", final_answer.timing)
    yield _sse_json_event("trace", final_answer.trace)
    yield "event: done\ndata: ok\n\n"


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
        )
    except Exception:
        LOGGER.exception("failed to persist chat conversation history")


def _file_source_items(result: dict) -> tuple[dict[str, Any], ...]:
    items = result.get("file_source_items")
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, dict))


def compute_final_answer(question: str, result: dict, conversation_id: str | None = None) -> FinalAnswer:
    client = GenosClient()
    timing = ensure_timing(result)
    if result.get("general_view_ready"):
        timing_payload = finish(timing)
        answer = enforce_answer_contract(
            question,
            cleanup_markdown_answer(str(result.get("answer") or "")),
            None,
            result.get("general_view_contract"),
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
        timing_payload = finish(timing)
        answer = cleanup_markdown_answer(str(result.get("answer") or ""))
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
    try:
        with stage(timing, "answer_generation_total", "GenOS expression plus safety"):
            generated_answer = "".join(client.stream_answer(question, result))
    except requests.RequestException:
        generated_answer = finalized_fallback_fact_answer(question, result.get("markdown_response"))
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
        safe_answer = apply_claim_policy(question, safe_answer, policy_fact_md)
    try:
        with stage(timing, "chart_generation", "fact-backed chart spec"):
            charts = build_charts(result, question=question, answer=safe_answer)
    except Exception:
        charts = []
    timing_payload = finish(timing)
    safe_answer = cleanup_markdown_answer(safe_answer)
    safe_answer = enforce_answer_contract(question, safe_answer, markdown_response, result.get("general_view_contract"))
    safe_answer = apply_claim_policy(question, safe_answer, policy_fact_md)
    safe_answer = enforce_answer_contract(question, safe_answer, markdown_response, result.get("general_view_contract"))
    if file_context_fact and _looks_like_empty_file_context_answer(safe_answer):
        safe_answer = apply_claim_policy(question, _file_context_fallback_answer(file_context_fact), policy_fact_md)
    safe_answer = _append_file_context_source(safe_answer, fact_md, file_context_fact)
    safe_answer = append_blocked_metric_notices_from_markdown_response(safe_answer, markdown_response)
    safe_answer = apply_common_unavailable_response(
        question,
        safe_answer,
        markdown_response,
        tool_calls=result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else (),
    )
    safe_answer = apply_requested_source_trap_gate(question, safe_answer)
    safe_answer = ensure_file_absence_statement(question, safe_answer, str(result.get("file_context") or ""))
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


def _sse_delta(token: str) -> str:
    lines = token.split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"event: delta\n{data}\n\n"


def _sse_json_event(event_name: str, payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    lines = data.split("\n")
    encoded = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_name}\n{encoded}\n\n"


app = create_app(startup_warmup=startup_warmup_from_env())
