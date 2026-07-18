from __future__ import annotations

import json

import requests

from jw_chat_agent_poc.tools.external import ExternalCall
from jw_chat_agent_poc.tools.external.cached_client import CachedExternalApiClient
from jw_chat_agent_poc.tools.external.result_cache import ExternalResultCache


class _McpResponse:
    def __init__(self, event: dict[str, object]) -> None:
        self.text = f"event: message\ndata: {json.dumps(event)}\n\n"

    def raise_for_status(self) -> None:
        return None


def _live_permission_response() -> _McpResponse:
    return _McpResponse(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [],
                "structuredContent": {
                    "result": [
                        {
                            "ITEM_SEQ": "200500287",
                            "ITEM_NAME": "리바로정1밀리그램",
                            "ITEM_PERMIT_DATE": "20050106",
                        }
                    ]
                },
            },
        }
    )


def test_live_mcp_success_is_shared_across_client_instances(monkeypatch) -> None:
    cache = ExternalResultCache(ttl_seconds=120, max_entries=8)
    calls = 0

    def fake_post(url, json, headers, timeout):
        nonlocal calls
        calls += 1
        return _live_permission_response()

    monkeypatch.setenv("NEDRUG_MCP_URL", "http://mcp-nedrug/mcp")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    first = CachedExternalApiClient(result_cache=cache).mfds_permission_search("리바로")
    second = CachedExternalApiClient(result_cache=cache).mfds_permission_search("리바로")

    assert calls == 1
    assert second is first


def test_live_mcp_cache_key_includes_normalized_arguments(monkeypatch) -> None:
    cache = ExternalResultCache(ttl_seconds=120, max_entries=8)
    calls = 0

    def fake_post(url, json, headers, timeout):
        nonlocal calls
        calls += 1
        return _live_permission_response()

    monkeypatch.setenv("NEDRUG_MCP_URL", "http://mcp-nedrug/mcp")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)
    client = CachedExternalApiClient(result_cache=cache)

    client.mfds_permission_search(" 리바로 ")
    client.mfds_permission_search("리바로")
    client.mfds_permission_search("리피토")

    assert calls == 2


def test_live_mcp_error_is_not_cached(monkeypatch) -> None:
    cache = ExternalResultCache(ttl_seconds=120, max_entries=8)
    calls = 0

    def fake_post(url, json, headers, timeout):
        nonlocal calls
        calls += 1
        raise requests.Timeout("temporary failure")

    monkeypatch.setenv("NEDRUG_MCP_URL", "http://mcp-nedrug/mcp")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)
    client = CachedExternalApiClient(result_cache=cache)

    first = client.mfds_permission_search("리바로")
    second = client.mfds_permission_search("리바로")

    assert first.status == "error"
    assert second.status == "error"
    assert calls == 4


def test_live_web_success_uses_normalized_query_cache_key(monkeypatch) -> None:
    cache = ExternalResultCache(ttl_seconds=120, max_entries=8)
    calls = 0

    def fake_post(url, headers, json, timeout):
        nonlocal calls
        calls += 1

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "results": [
                        {
                            "title": "리바로 근거",
                            "url": "https://example.test/livalo",
                            "content": "실제 검색 스니펫",
                        }
                    ]
                }

        return Response()

    monkeypatch.setenv("TAVILY_API_KEY", "SECRETKEY")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr("jw_chat_agent_poc.tools.external.client.requests.post", fake_post)

    first = CachedExternalApiClient(result_cache=cache).web_search(" 리바로   임상 ", max_results=9)
    second = CachedExternalApiClient(result_cache=cache).web_search("리바로 임상", max_results=5)

    assert calls == 1
    assert second is first


def test_external_result_cache_expires_successful_calls() -> None:
    now = [10.0]
    cache = ExternalResultCache(ttl_seconds=2, max_entries=8, clock=lambda: now[0])
    call = ExternalCall(
        tool="web_search",
        source="web_search",
        status="live",
        summary_text="1건",
        render_data={"items": [{"title": "근거"}]},
    )

    cache.put(("web", "tavily", "리바로"), call)
    assert cache.get(("web", "tavily", "리바로")) is call

    now[0] = 12.0
    assert cache.get(("web", "tavily", "리바로")) is None


def test_external_result_cache_rejects_non_live_and_empty_calls() -> None:
    cache = ExternalResultCache(ttl_seconds=120, max_entries=8)
    error = ExternalCall(
        tool="web_search",
        source="web_search",
        status="error",
        summary_text="실패",
        render_data={"items": []},
    )
    empty = ExternalCall(
        tool="web_search",
        source="web_search",
        status="live",
        summary_text="잘못 표시된 빈 성공",
        render_data={"items": []},
    )

    cache.put(("error",), error)
    cache.put(("empty",), empty)

    assert cache.get(("error",)) is None
    assert cache.get(("empty",)) is None
