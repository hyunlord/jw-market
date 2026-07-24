from __future__ import annotations

import json
from typing import Any

import pytest

from jw_chat_agent_poc.tools.external.client import ExternalApiClient
from jw_chat_agent_poc.tools.external.mcp_client import McpClientError, McpJsonClient, McpToolResult


class _McpResponse:
    def __init__(self, result: dict[str, Any]) -> None:
        event = {"jsonrpc": "2.0", "id": 1, "result": result}
        self.text = f"event: message\ndata: {json.dumps(event)}\n\n"

    def raise_for_status(self) -> None:
        return None


def _tool_schema(name: str, bounded_field: str, *, maximum: int = 20) -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": name,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        bounded_field: {
                            "type": "number",
                            "minimum": 1,
                            "maximum": maximum,
                        }
                    },
                },
            }
        ]
    }


@pytest.mark.parametrize(
    ("tool_name", "bounded_field"),
    [
        ("search_studies", "pageSize"),
        ("search_drug_labels", "limit"),
        ("search_drug_adverse_events", "limit"),
    ],
)
def test_guard_rejects_zero_before_tools_call(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    bounded_field: str,
) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kwargs: Any) -> _McpResponse:
        requests_seen.append(json)
        return _McpResponse(_tool_schema(tool_name, bounded_field))

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    with pytest.raises(McpClientError, match=rf"{tool_name}.*{bounded_field}.*minimum 1"):
        McpJsonClient("http://mcp.test/mcp").call_tool_checked(
            tool_name,
            {bounded_field: 0},
        )

    assert [request["method"] for request in requests_seen] == ["tools/list"]


@pytest.mark.parametrize(
    ("tool_name", "bounded_field", "value", "maximum"),
    [
        ("search_studies", "pageSize", 1, 20),
        ("search_studies", "pageSize", 20, 20),
        ("search_drug_labels", "limit", 1, 20),
        ("search_drug_labels", "limit", 20, 20),
        ("search_drug_adverse_events", "limit", 1, 1000),
        ("search_drug_adverse_events", "limit", 1000, 1000),
    ],
)
def test_guard_preserves_valid_values(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    bounded_field: str,
    value: int,
    maximum: int,
) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kwargs: Any) -> _McpResponse:
        requests_seen.append(json)
        if json["method"] == "tools/list":
            return _McpResponse(_tool_schema(tool_name, bounded_field, maximum=maximum))
        return _McpResponse({"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    result = McpJsonClient("http://mcp.test/mcp").call_tool_checked(
        tool_name,
        {bounded_field: value},
    )

    assert result.content_text == "ok"
    assert [request["method"] for request in requests_seen] == ["tools/list", "tools/call"]
    assert requests_seen[1]["params"]["arguments"][bounded_field] == value


def test_guard_uses_schema_for_other_bounded_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    requests_seen: list[dict[str, Any]] = []
    schema = {
        "tools": [
            {
                "name": "search_drug_labels",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "number", "minimum": 1, "maximum": 20},
                        "skip": {"type": "number", "minimum": 0},
                    },
                },
            }
        ]
    }

    def fake_post(_url: str, *, json: dict[str, Any], **_kwargs: Any) -> _McpResponse:
        requests_seen.append(json)
        return _McpResponse(schema)

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    with pytest.raises(McpClientError, match=r"search_drug_labels.*skip.*minimum 0"):
        McpJsonClient("http://mcp.test/mcp").call_tool_checked(
            "search_drug_labels",
            {"limit": 5, "skip": -1},
        )

    assert [request["method"] for request in requests_seen] == ["tools/list"]


def test_guard_rejects_values_above_the_schema_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kwargs: Any) -> _McpResponse:
        requests_seen.append(json)
        return _McpResponse(_tool_schema("search_studies", "pageSize"))

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    with pytest.raises(McpClientError, match=r"search_studies.*pageSize.*maximum 20"):
        McpJsonClient("http://mcp.test/mcp").call_tool_checked(
            "search_studies",
            {"pageSize": 21},
        )

    assert [request["method"] for request in requests_seen] == ["tools/list"]


def test_live_client_applies_guard_only_to_the_three_audited_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def checked(_self: McpJsonClient, name: str, arguments: dict[str, Any]) -> McpToolResult:
        calls.append(("checked", name, arguments))
        return McpToolResult(content_text="", raw_result={"structuredContent": {"result": {}}})

    def unchecked(_self: McpJsonClient, name: str, arguments: dict[str, Any]) -> McpToolResult:
        calls.append(("unchecked", name, arguments))
        return McpToolResult(content_text="", raw_result={"structuredContent": {"result": {}}})

    monkeypatch.setattr(McpJsonClient, "call_tool_checked", checked)
    monkeypatch.setattr(McpJsonClient, "call_tool", unchecked)
    client = ExternalApiClient(mode="live")

    client._live_mcp_call("clinicaltrials_v2_search", {"query.condition": "diabetes"})
    client._live_mcp_call("openfda_label_search", {"search": 'openfda.substance_name:"PITAVASTATIN"'})
    client._live_mcp_call(
        "openfda_label_search",
        {
            "search": 'openfda.substance_name:"PITAVASTATIN"',
            "evidence_type": "adverse_event",
        },
    )
    client._live_mcp_call("mfds_permission_search", {"brand": "리바로"})

    assert [call[:2] for call in calls] == [
        ("checked", "search_studies"),
        ("checked", "search_drug_labels"),
        ("checked", "search_drug_adverse_events"),
        ("unchecked", "search_drug_permission_list"),
    ]
