from __future__ import annotations

import json
from functools import lru_cache
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from jw_chat_agent_poc.agent_loop import should_use_agent_loop
from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies, build_tool_use_agent, unsupported_brand_result
from jw_chat_agent_poc.orchestrator import ChatAgent
from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract
from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_labels
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.resolver import UnsupportedBrandError
from jw_chat_agent_poc.service.answer_safety import (
    cleanup_markdown_answer,
    ensure_top_brand_trend_table,
    finalized_fallback_fact_answer,
)
from jw_chat_agent_poc.service.charts import build_charts
from jw_chat_agent_poc.service.conversation import ConversationStore, PendingClarification
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.models import ChatAccepted, ChatRequest, HealthResponse
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope, version_payload
from jw_chat_agent_poc.service.sse_protocol import iter_markdown_sse_events
from jw_chat_agent_poc.common.timing import ensure_timing, finish, stage
from jw_chat_agent_poc.tools.metrics.market_scope import (
    MarketScopeResolver,
    detect_market_scope_intent,
    map_market_view_reply,
)


AgentFactory = Callable[..., ChatAgent]


class SessionStore:
    def __init__(self, conversations: ConversationStore | None = None) -> None:
        self._items: dict[str, dict] = {}
        self.conversations = conversations or ConversationStore()

    def put(self, item: dict) -> str:
        session_id = uuid4().hex
        self._items[session_id] = item
        return session_id

    def get(self, session_id: str) -> dict | None:
        return self._items.get(session_id)


def create_app(
    *,
    agent_factory: AgentFactory | None = None,
    market_scope_resolver: MarketScopeResolver | None = None,
    store: SessionStore | None = None,
) -> FastAPI:
    app = FastAPI(title="JW Chat Agent POC", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    store = store or SessionStore()
    resolver = market_scope_resolver or MarketScopeResolver()
    make_agent = agent_factory or _default_agent_factory
    use_direct_agent_loop = agent_factory is None

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/__version")
    def version() -> dict:
        return version_payload()

    @app.post("/chat", response_model=ChatAccepted)
    def chat(request: ChatRequest) -> ChatAccepted:
        documents = tuple(Path(path) for path in request.document_paths)
        result = _answer_question(
            store,
            resolver,
            make_agent,
            request.question,
            request.external_mode,
            request.conversation_id,
            list(documents),
            use_direct_agent_loop=use_direct_agent_loop,
        )
        session_id = store.put({"question": request.question, "result": result["result"]})
        return ChatAccepted(
            session_id=session_id,
            conversation_id=result["conversation_id"],
            sources=tuple(result["result"].get("sources", ())),
        )

    @app.get("/chat/stream")
    def chat_stream(
        session_id: str | None = Query(default=None),
        question: str | None = Query(default=None),
        external_mode: str = Query(default="live"),
        conversation_id: str | None = Query(default=None),
    ) -> StreamingResponse:
        item = _resolve_session(
            store,
            resolver,
            make_agent,
            session_id,
            question,
            external_mode,
            conversation_id,
            use_direct_agent_loop=use_direct_agent_loop,
        )
        return StreamingResponse(
            _sse_events(item["question"], item["result"], item.get("conversation_id")),
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
    *,
    use_direct_agent_loop: bool = False,
) -> dict:
    state = store.conversations.get_or_create(conversation_id)
    result = _answer_with_conversation(
        store,
        market_scope_resolver,
        agent_factory,
        state.conversation_id,
        question,
        external_mode,
        documents,
        use_direct_agent_loop=use_direct_agent_loop,
    )
    store.conversations.record_exchange(state.conversation_id, question, str(result.get("answer") or ""), _applied_filters(result))
    return {"question": question, "result": result, "conversation_id": state.conversation_id}


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
    return _answer_without_pending(
        market_scope_resolver,
        agent_factory,
        conversation_id,
        question,
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
    if use_direct_agent_loop and should_use_agent_loop(question) and not documents:
        return _answer_direct_agent_loop(question, external_mode)
    if should_use_agent_loop(question):
        return agent_factory(external_mode=external_mode).answer(question, documents)
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
    return agent_factory(external_mode=external_mode).answer(question, documents)


def _answer_direct_agent_loop(question: str, external_mode: str) -> dict:
    dependencies = build_chat_agent_dependencies(external_mode=external_mode)
    try:
        dependencies.resolver.resolve(question, allow_default=False)
    except UnsupportedBrandError:
        routes = dependencies.router.route(question, has_documents=False)
        return unsupported_brand_result(question, routes, router_diagnostics(dependencies.router))
    return build_tool_use_agent(dependencies.agent_loop_dependencies()).answer(question)


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


def _sse_events(question: str, result: dict, conversation_id: str | None = None):
    if conversation_id:
        yield f"event: conversation\ndata: {conversation_id}\n\n"
    yield f"event: sources\ndata: {','.join(source_labels(result.get('sources', [])))}\n\n"
    client = GenosClient()
    timing = ensure_timing(result)
    try:
        with stage(timing, "answer_generation_total", "GenOS expression plus safety"):
            generated_answer = "".join(client.stream_answer(question, result))
    except requests.RequestException:
        generated_answer = finalized_fallback_fact_answer(question, result.get("markdown_response"))
    with stage(timing, "answer_cleanup", "markdown cleanup"):
        safe_answer = cleanup_markdown_answer(generated_answer)
        markdown_response = result.get("markdown_response")
        fact_md = ""
        if isinstance(markdown_response, dict):
            fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
            safe_answer = ensure_top_brand_trend_table(safe_answer, fact_md)
        safe_answer = apply_claim_policy(question, safe_answer, fact_md)
    try:
        with stage(timing, "chart_generation", "fact-backed chart spec"):
            charts = build_charts(result, question=question, answer=safe_answer)
    except Exception:
        charts = []
    timing_payload = finish(timing)
    safe_answer = cleanup_markdown_answer(safe_answer)
    safe_answer = enforce_answer_contract(question, safe_answer, markdown_response)
    safe_answer = apply_claim_policy(question, safe_answer, fact_md)
    yield from iter_markdown_sse_events(safe_answer)
    if charts:
        yield _sse_json_event("charts", charts)
    yield _sse_json_event("timing", timing_payload)
    yield _sse_json_event(
        "trace",
        trace_envelope(
            question=question,
            result=result,
            answer=safe_answer,
            charts=charts,
            timing=timing_payload,
            conversation_id=conversation_id,
        ),
    )
    yield "event: done\ndata: ok\n\n"


def _sse_delta(token: str) -> str:
    lines = token.split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"event: delta\n{data}\n\n"


def _sse_json_event(event_name: str, payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    lines = data.split("\n")
    encoded = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event_name}\n{encoded}\n\n"


app = create_app()
