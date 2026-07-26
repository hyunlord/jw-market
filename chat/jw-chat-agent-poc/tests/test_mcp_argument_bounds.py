from __future__ import annotations

import json
from typing import Any

import pytest

from jw_chat_agent_poc.tools.external.client import ExternalApiClient
from jw_chat_agent_poc.tools.external.mcp_client import (
    McpArgumentValidationError,
    McpJsonClient,
)


class _McpResponse:
    def __init__(self, result: dict[str, Any]) -> None:
        event = {"jsonrpc": "2.0", "id": 1, "result": result}
        self.text = f"event: message\ndata: {json.dumps(event)}\n\n"

    def raise_for_status(self) -> None:
        return None


def _tool_schema(name: str, field: str, *, maximum: int = 20) -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": name,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        field: {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": maximum,
                        }
                    },
                },
            }
        ]
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (0, "below schema minimum 1"),
        (-1, "below schema minimum 1"),
        (21, "above schema maximum 20"),
        ("5", "must be numeric"),
        (None, "must be numeric"),
        (True, "must be numeric"),
    ],
)
def test_checked_call_rejects_invalid_bound_before_tool_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
    message: str,
) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kwargs: Any) -> _McpResponse:
        requests_seen.append(json)
        return _McpResponse(_tool_schema("search_studies", "pageSize"))

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    with pytest.raises(McpArgumentValidationError, match=message):
        McpJsonClient("http://mcp.test/mcp").call_tool_checked(
            "search_studies",
            {"pageSize": value},
        )

    assert [request["method"] for request in requests_seen] == ["tools/list"]


@pytest.mark.parametrize("value", [1, 5, 20])
def test_checked_call_preserves_valid_schema_bounds(
    monkeypatch: pytest.MonkeyPatch,
    value: int,
) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(_url: str, *, json: dict[str, Any], **_kwargs: Any) -> _McpResponse:
        requests_seen.append(json)
        if json["method"] == "tools/list":
            return _McpResponse(_tool_schema("search_studies", "pageSize"))
        return _McpResponse({"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("jw_chat_agent_poc.tools.external.mcp_client.requests.post", fake_post)

    result = McpJsonClient("http://mcp.test/mcp").call_tool_checked(
        "search_studies",
        {"pageSize": value},
    )

    assert result.content_text == "ok"
    assert [request["method"] for request in requests_seen] == ["tools/list", "tools/call"]


def test_live_client_uses_schema_guard_only_for_audited_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def checked(self: McpJsonClient, name: str, arguments: dict[str, Any]) -> Any:
        calls.append(("checked", name))
        return self.call_tool(name, arguments)

    original_call_tool = McpJsonClient.call_tool

    def unchecked(self: McpJsonClient, name: str, arguments: dict[str, Any]) -> Any:
        calls.append(("unchecked", name))
        return original_call_tool(self, name, arguments)

    monkeypatch.setattr(McpJsonClient, "call_tool_checked", checked)
    monkeypatch.setattr(McpJsonClient, "call_tool", unchecked)
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.requests.post",
        lambda *_args, **_kwargs: _McpResponse(
            {"content": [{"type": "text", "text": "{}"}]}
        ),
    )

    client = ExternalApiClient(mode="live")
    client._live_mcp_call("clinicaltrials_v2_search", {"query.condition": "diabetes"})
    client._live_mcp_call("openfda_label_search", {"search": "pitavastatin"})
    client._live_mcp_call(
        "openfda_label_search",
        {"search": "pitavastatin", "evidence_type": "adverse_event"},
    )
    client._live_mcp_call("mfds_permission_search", {"brand": "리바로"})

    assert calls == [
        ("checked", "search_studies"),
        ("unchecked", "search_studies"),
        ("checked", "search_drug_labels"),
        ("unchecked", "search_drug_labels"),
        ("checked", "search_drug_adverse_events"),
        ("unchecked", "search_drug_adverse_events"),
        ("unchecked", "search_drug_permission_list"),
    ]
