from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import time
from typing import Any, Final, Iterator

import requests


MCP_ACCEPT_HEADER: Final[str] = "application/json, text/event-stream"
MCP_FIRST_ATTEMPT_TIMEOUT_S: Final[int] = 5
MCP_WRAPPER_GRACE_S: Final[float] = 1.0
_MCP_EXECUTION_DEADLINE: ContextVar[float | None] = ContextVar(
    "mcp_execution_deadline",
    default=None,
)
_MCP_MAX_ATTEMPTS: ContextVar[int] = ContextVar("mcp_max_attempts", default=2)


@dataclass(frozen=True, slots=True)
class McpToolResult:
    content_text: str
    raw_result: dict[str, Any]


class McpClientError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class McpArgumentValidationError(McpClientError):
    """Raised before dispatch when arguments violate the MCP tool schema."""


@contextmanager
def mcp_execution_budget(timeout_s: float) -> Iterator[None]:
    grace_s = min(MCP_WRAPPER_GRACE_S, max(float(timeout_s) * 0.1, 0.001))
    deadline = time.monotonic() + max(float(timeout_s) - grace_s, 0.001)
    token = _MCP_EXECUTION_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _MCP_EXECUTION_DEADLINE.reset(token)


@contextmanager
def mcp_attempt_limit(max_attempts: int) -> Iterator[None]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    token = _MCP_MAX_ATTEMPTS.set(max_attempts)
    try:
        yield
    finally:
        _MCP_MAX_ATTEMPTS.reset(token)


class McpJsonClient:
    def __init__(
        self,
        url: str,
        timeout_s: float = 12,
        *,
        connect_timeout_s: float | None = None,
        first_attempt_timeout_s: float | None = None,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.first_attempt_timeout_s = first_attempt_timeout_s

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

    def call_tool_checked(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        schema = self._tool_input_schema(name)
        _validate_numeric_bounds(name, arguments, schema)
        return self.call_tool(name, arguments)

    def _tool_input_schema(self, name: str) -> dict[str, Any]:
        tools = self._post("tools/list", {}).get("tools")
        if not isinstance(tools, list):
            raise McpClientError("MCP tools/list did not include a tools array")
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") != name:
                continue
            schema = tool.get("inputSchema")
            if isinstance(schema, dict):
                return schema
            raise McpClientError(f"MCP tool {name} did not include an input schema")
        raise McpClientError(f"MCP tool {name} was not present in tools/list")

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last_error: Exception | None = None
        max_attempts = _MCP_MAX_ATTEMPTS.get()
        for attempt in range(max_attempts):
            requested_timeout_s = (
                self.first_attempt_timeout_s or MCP_FIRST_ATTEMPT_TIMEOUT_S
                if attempt == 0
                else self.timeout_s
            )
            timeout_s = _remaining_timeout_s(requested_timeout_s)
            request_timeout: float | tuple[float, float] = timeout_s
            if self.connect_timeout_s is not None:
                request_timeout = (min(self.connect_timeout_s, timeout_s), timeout_s)
            try:
                response = requests.post(
                    self.url,
                    json=payload,
                    headers={"Accept": MCP_ACCEPT_HEADER},
                    timeout=request_timeout,
                )
                response.raise_for_status()
                response.encoding = "utf-8"
                event = _first_sse_event(response.text)
                if "error" in event:
                    raise McpClientError(_mcp_error_message(event["error"]))
                result = event.get("result")
                if not isinstance(result, dict):
                    raise McpClientError("MCP response did not include a result object")
                return result
            except (requests.RequestException, McpClientError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    time.sleep(_remaining_timeout_s(0.2))
        raise McpClientError(str(last_error) if last_error else "MCP request failed")


def _validate_numeric_bounds(
    tool_name: str,
    arguments: dict[str, Any],
    input_schema: dict[str, Any],
) -> None:
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        raise McpClientError(f"MCP tool {tool_name} input schema did not include properties")
    for field, value in arguments.items():
        field_schema = properties.get(field)
        if not isinstance(field_schema, dict):
            continue
        minimum = field_schema.get("minimum")
        maximum = field_schema.get("maximum")
        if minimum is None and maximum is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise McpArgumentValidationError(
                f"MCP tool {tool_name} argument {field} must be numeric"
            )
        if isinstance(minimum, (int, float)) and value < minimum:
            raise McpArgumentValidationError(
                f"MCP tool {tool_name} argument {field} is below schema minimum {minimum}"
            )
        if isinstance(maximum, (int, float)) and value > maximum:
            raise McpArgumentValidationError(
                f"MCP tool {tool_name} argument {field} is above schema maximum {maximum}"
            )


def _remaining_timeout_s(requested_timeout_s: float) -> float:
    deadline = _MCP_EXECUTION_DEADLINE.get()
    if deadline is None:
        return float(requested_timeout_s)
    remaining_s = deadline - time.monotonic()
    if remaining_s <= 0:
        raise McpClientError("MCP execution deadline exceeded")
    return max(min(float(requested_timeout_s), remaining_s), 0.001)


def _first_sse_event(text: str) -> dict[str, Any]:
    data_fields: list[str] = []
    for line in [*text.splitlines(), ""]:
        if not line:
            payload = "\n".join(data_fields).strip()
            data_fields = []
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if isinstance(event, dict):
                return event
            continue
        if line.startswith("data:"):
            data_fields.append(line.split(":", 1)[1].lstrip())
            continue
        if data_fields and not line.startswith(("event:", "id:", "retry:", ":")):
            # Some MCP bundles emit raw line continuations without an SSE field.
            data_fields[-1] += line
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
