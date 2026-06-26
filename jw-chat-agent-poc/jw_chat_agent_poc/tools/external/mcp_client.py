from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Final

import requests


MCP_ACCEPT_HEADER: Final[str] = "application/json, text/event-stream"


@dataclass(frozen=True, slots=True)
class McpToolResult:
    content_text: str
    raw_result: dict[str, Any]


class McpClientError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class McpJsonClient:
    def __init__(self, url: str, timeout_s: int = 12) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        result = self._post(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        content = result.get("content")
        texts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    texts.append(item["text"])
        return McpToolResult(content_text="\n".join(texts).strip(), raw_result=result)

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = requests.post(
                    self.url,
                    json=payload,
                    headers={"Accept": MCP_ACCEPT_HEADER},
                    timeout=self.timeout_s,
                )
                response.raise_for_status()
                event = _first_sse_event(response.text)
                if "error" in event:
                    raise McpClientError(_mcp_error_message(event["error"]))
                result = event.get("result")
                if not isinstance(result, dict):
                    raise McpClientError("MCP response did not include a result object")
                return result
            except (requests.RequestException, McpClientError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.2)
        raise McpClientError(str(last_error) if last_error else "MCP request failed")


def _first_sse_event(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line.split(":", 1)[1].strip()
        if not payload or payload == "[DONE]":
            continue
        event = json.loads(payload)
        if isinstance(event, dict):
            return event
    raise McpClientError("MCP response did not include an SSE data event")


def _mcp_error_message(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if message and code is not None:
            return f"{code}: {message}"
        if message:
            return str(message)
    return str(error)
