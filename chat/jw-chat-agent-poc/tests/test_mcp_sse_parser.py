from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from jw_chat_agent_poc.tools.external.mcp_client import (
    McpJsonClient,
    _first_sse_event,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(body: bytes, *, encoding: str = "ISO-8859-1") -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.encoding = encoding
    return response


def test_214_raw_continuation_is_reassembled() -> None:
    text = _fixture_bytes("mcp214_tavily_raw_continuation.sse").decode("utf-8")

    event = _first_sse_event(text)

    item = event["result"]["structuredContent"]["result"]["results"][0]
    assert item["title"] == "리바로 관련 기사"
    assert item["url"] == "https://www.bosa.co.kr/news/articleView.html?idxno=2261590"


def test_standard_multiple_data_fields_are_joined_per_sse() -> None:
    text = (
        "event: message\n"
        'data: {"jsonrpc":"2.0",\n'
        'data: "id":1,\n'
        'data: "result":{"tools":[]}}\n\n'
    )

    assert _first_sse_event(text) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": []},
    }


def test_hira_three_line_control_remains_parseable() -> None:
    text = _fixture_bytes("mcp_hira_three_line.sse").decode("utf-8")

    event = _first_sse_event(text)

    assert event["result"]["tools"] == [{"name": "hira_lookup"}]


def test_done_event_is_skipped_before_first_json_event() -> None:
    text = (
        "data: [DONE]\n\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
    )

    assert _first_sse_event(text)["result"] == {"tools": []}


def test_mcp_response_is_forced_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    event: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"title": "리바로 관련 기사"},
    }
    response = _response(
        f"event: message\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
    )
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.requests.post",
        lambda *_args, **_kwargs: response,
    )

    result = McpJsonClient("http://mcp.test/json")._post("tools/list", {})

    assert response.encoding == "utf-8"
    assert result["title"] == "리바로 관련 기사"


def test_corrupt_payload_still_raises_json_decode_error() -> None:
    with pytest.raises(json.JSONDecodeError):
        _first_sse_event("event: message\ndata: {not-json}\n\n")
