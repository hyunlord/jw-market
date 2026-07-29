from __future__ import annotations

import json
from typing import Any

from jw_chat_agent_poc.tools.external.client import (
    ExternalApiClient,
    ExternalCall,
    _mcp_tool_spec,
)


class _McpResponse:
    def __init__(self, result: dict[str, Any]) -> None:
        event = {"jsonrpc": "2.0", "id": 1, "result": result}
        self.text = f"event: message\ndata: {json.dumps(event)}\n\n"

    def raise_for_status(self) -> None:
        return None


def test_default_web_search_provider_remains_direct_tavily_rest(monkeypatch) -> None:
    calls: list[tuple[str, int, str]] = []

    def fake_tavily(
        _self: ExternalApiClient,
        query: str,
        max_results: int,
        *,
        topic: str,
    ) -> ExternalCall:
        calls.append((query, max_results, topic))
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="live",
            summary_text="rest",
            render_data={"provider": "tavily", "items": []},
        )

    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(ExternalApiClient, "_live_tavily_search", fake_tavily)

    call = ExternalApiClient(mode="live").web_search("리바로 뉴스", topic="news")

    assert calls == [("리바로 뉴스", 5, "news")]
    assert call.summary_text == "rest"


def test_tavily_mcp_spec_uses_resource_214_and_advanced_depth(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_MCP_RESOURCE_ID", raising=False)

    spec = _mcp_tool_spec(
        "tavily_mcp_search",
        {"query": "리바로 뉴스", "max_results": "3", "topic": "news"},
    )

    assert spec == {
        "resource_id": "214",
        "source": "tavily_mcp",
        "mcp_tool": "tavily_search",
        "arguments": {
            "query": "리바로 뉴스",
            "max_results": 3,
            "search_depth": "advanced",
            "topic": "news",
        },
    }
    assert "basic" not in spec["arguments"].values()


def test_tavily_mcp_url_falls_back_to_gateway(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_MCP_URL", raising=False)
    monkeypatch.delenv("GENOS_MCP_GATEWAY_BASE", raising=False)
    client = ExternalApiClient(mode="live")

    assert (
        client._mcp_url("214", "tavily_mcp")
        == "http://llmops-gateway-api-service:8080/mcp/214/mcp"
    )


def test_tavily_mcp_direct_url_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_MCP_URL", "http://code-serving-214:8080/")
    client = ExternalApiClient(mode="live")

    assert client._mcp_url("214", "tavily_mcp") == "http://code-serving-214:8080"


def test_tavily_mcp_provider_uses_real_client_contract_and_projects_web_schema(
    monkeypatch,
) -> None:
    requests_seen: list[tuple[str, dict[str, Any]]] = []

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        **_kwargs: Any,
    ) -> _McpResponse:
        requests_seen.append((url, json))
        return _McpResponse(
            {
                "content": [],
                "structuredContent": {
                    "result": {
                        "results": [
                            {
                                "title": "리바로 관련 기사",
                                "url": "https://example.test/livalo",
                                "content": "기사 요약",
                                "published_date": "2026-07-29",
                            }
                        ]
                    }
                },
            }
        )

    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily_mcp")
    monkeypatch.setenv("TAVILY_MCP_URL", "http://code-serving-214:8080")
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.requests.post",
        fake_post,
    )

    call = ExternalApiClient(mode="live").web_search(
        "리바로 뉴스",
        max_results=3,
        topic="news",
    )

    assert len(requests_seen) == 1
    url, payload = requests_seen[0]
    assert url == "http://code-serving-214:8080"
    assert payload["method"] == "tools/call"
    assert payload["params"] == {
        "name": "tavily_search",
        "arguments": {
            "query": "리바로 뉴스",
            "max_results": 3,
            "search_depth": "advanced",
            "topic": "news",
        },
    }
    assert call.tool == "web_search"
    assert call.source == "web_search"
    assert call.status == "live"
    assert call.render_data["provider"] == "tavily_mcp"
    assert call.render_data["items"] == [
        {
            "title": "리바로 관련 기사",
            "url": "https://example.test/livalo",
            "snippet": "기사 요약",
            "published_date": "2026-07-29",
        }
    ]


def test_existing_clinical_trials_mcp_spec_is_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("CLINICAL_TRIALS_MCP_RESOURCE_ID", raising=False)

    assert _mcp_tool_spec(
        "clinicaltrials_v2_search",
        {"query.intr": "pitavastatin"},
    ) == {
        "resource_id": "169",
        "source": "clinicaltrials_mcp",
        "mcp_tool": "search_studies",
        "arguments": {"intervention": "pitavastatin", "pageSize": 5},
    }
