from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    RequestedAnswerShape,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.retrieval_events import (
    classify_failure_signals,
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.tools.external.mcp_client import McpClientError, McpToolResult


def _external() -> SimpleNamespace:
    return SimpleNamespace(
        timeout_s=12,
        _mcp_url=lambda _resource_id, _source: "http://code-serving-214:8080/json",
    )


def _success_result() -> McpToolResult:
    return McpToolResult(
        content_text="",
        raw_result={
            "structuredContent": {
                "result": {
                    "results": [
                        {
                            "title": "synthetic result",
                            "url": "https://example.test/result",
                            "content": "synthetic content",
                        }
                    ]
                }
            }
        },
    )


def test_web_mcp_uses_measured_timeout_env_without_changing_shared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, url: str, **kwargs: Any) -> None:
            captured.update({"url": url, **kwargs})

        def call_tool(self, _name: str, _arguments: dict[str, Any]) -> McpToolResult:
            return _success_result()

    monkeypatch.setenv("WEB_SEARCH_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("WEB_SEARCH_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("WEB_SEARCH_MAX_CONCURRENCY", "2")
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.McpJsonClient",
        Client,
    )

    call = v4_adapters._v4_tavily_mcp_request(
        _external(),
        "리바로젯 특허현황",
        search_depth="advanced",
        topic="general",
    )

    assert call.status == "live"
    assert captured == {
        "url": "http://code-serving-214:8080/json",
        "timeout_s": 8.0,
        "connect_timeout_s": 2.0,
        "first_attempt_timeout_s": 8.0,
    }


def test_web_concurrency_limit_is_local_to_tavily_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    class Client:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            pass

        def call_tool(self, _name: str, _arguments: dict[str, Any]) -> McpToolResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return _success_result()

    monkeypatch.setenv("WEB_SEARCH_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("WEB_SEARCH_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("WEB_SEARCH_MAX_CONCURRENCY", "2")
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.McpJsonClient",
        Client,
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        calls = tuple(
            pool.map(
                lambda index: v4_adapters._v4_tavily_mcp_request(
                    _external(),
                    f"synthetic {index}",
                    search_depth="advanced",
                    topic="general",
                ),
                range(4),
            )
        )

    assert all(call.status == "live" for call in calls)
    assert peak == 2


def test_short_read_timeout_is_typed_as_incomplete_not_zero_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self, _url: str, **_kwargs: Any) -> None:
            pass

        def call_tool(self, _name: str, _arguments: dict[str, Any]) -> McpToolResult:
            raise McpClientError(
                "HTTPConnectionPool: Read timed out. (read timeout=0.01)"
            )

    monkeypatch.setenv("WEB_SEARCH_CONNECT_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("WEB_SEARCH_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("WEB_SEARCH_MAX_CONCURRENCY", "2")
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.McpJsonClient",
        Client,
    )

    call = v4_adapters._v4_tavily_mcp_request(
        _external(),
        "synthetic timeout",
        search_depth="advanced",
        topic="general",
    )
    status = classify_failure_signals(
        (call.status, str(call.render_data.get("status") or "")),
        call.summary_text,
    )
    event = retrieval_event_from_result(
        SourceResult(
            source="web",
            query="synthetic timeout",
            status=status,
            notice=call.summary_text,
        )
    )
    surface = public_retrieval_notice(event)

    assert call.render_data["error_type"] == "read_timeout"
    assert call.render_data["status"] == "timeout"
    assert event.status == "timeout"
    assert "조회가 완료되지 않아 확인할 수 없습니다" in surface
    assert "0건" not in surface
    assert "조회 결과가 없습니다" not in surface


def test_soft_deadline_keeps_transport_request_in_execution_trace() -> None:
    def empty(source: str):
        return lambda query: SourceResult(source=source, query=query, status="empty")

    def mart(query: str) -> SourceResult:
        return SourceResult(source="mart", query=query, status="ok", payload={"rows": [1]})

    def web(
        query: str,
        *,
        transport_event_callback=None,
    ) -> SourceResult:
        transport_event_callback(
            {
                "attempt": 1,
                "phase": "request_issued",
                "request_issued": True,
                "response_received": False,
                "status": "in_flight",
            }
        )
        time.sleep(0.08)
        transport_event_callback(
            {
                "attempt": 1,
                "phase": "attempt_completed",
                "request_issued": True,
                "response_received": True,
                "status": "ok",
                "error_type": None,
            }
        )
        return SourceResult(source="web", query=query, status="ok")

    setattr(web, "supports_transport_event_callback", True)
    adapters = {
        "mart": mart,
        "nedrug": empty("nedrug"),
        "hira": empty("hira"),
        "openfda": empty("openfda"),
        "clinicaltrials": empty("clinicaltrials"),
        "web": web,
        "patent": empty("patent"),
    }
    plan = PlannerOutput(
        resolved_question="synthetic",
        expanded_intents=("synthetic",),
        answer_sources=("mart",),
        tool_queries=ToolQueries(
            mart=("synthetic",),
            nedrug=("synthetic",),
            hira=("synthetic",),
            openfda=("synthetic",),
            clinicaltrials=("synthetic",),
            web=("synthetic",),
            patent=("synthetic",),
        ),
        linking_plan="first hop is sufficient",
        requested_answer_shape=RequestedAnswerShape(),
    )

    outcome = ParallelSourceExecutor(
        adapters=adapters,
        per_tool_timeout_s=1.0,
        total_timeout_s=1.0,
    ).execute_with_trace(
        plan,
        session_id="soft-deadline-transport-trace",
        answer_sources=("mart",),
        soft_deadline_s=0.02,
        source_filter=("mart", "web"),
    )

    web_trace = next(item for item in outcome.trace["tools"] if item["source"] == "web")
    assert web_trace["status"] == "timeout"
    assert web_trace["exclusion_reason"] == "soft_deadline_after_answer_quorum"
    assert web_trace["web_transport"]["requests_issued"] == 1
    assert web_trace["web_transport"]["responses_received"] == 0
    assert web_trace["web_transport"]["pending_attempts"] == 1
    assert web_trace["web_transport"]["retry_count"] == 0

    time.sleep(0.09)
    assert web_trace["web_transport"]["responses_received"] == 1
    assert web_trace["web_transport"]["pending_attempts"] == 0
    assert web_trace["web_transport"]["credit_at_risk_without_response"] == 0
