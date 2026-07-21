from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
import requests

from jw_chat_agent_poc.genos_config import resolve_planner_genos_base_url, resolve_planner_genos_token


DEFAULT_TOOL_ROUTING_PLANNER_TIMEOUT_S = 20.0
DEFAULT_TOOL_ROUTING_PLANNER_MAX_TOKENS = 512
TOOL_ROUTING_PLANNER_MAX_TOKENS_FLAG = "CHAT_TOOL_ROUTING_PLANNER_MAX_TOKENS"


def configured_planner_max_tokens() -> int:
    raw = os.environ.get(
        TOOL_ROUTING_PLANNER_MAX_TOKENS_FLAG,
        str(DEFAULT_TOOL_ROUTING_PLANNER_MAX_TOKENS),
    )
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_TOOL_ROUTING_PLANNER_MAX_TOKENS
    return min(max(value, 64), 4096)


class ToolProviderConfigurationError(RuntimeError):
    """Raised when the planner endpoint cannot satisfy the tool contract."""


class _FunctionCall(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    arguments: str = "{}"


class _ProviderToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str | None = None
    function: _FunctionCall


class _AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    content: str | None = None
    tool_calls: tuple[_ProviderToolCall, ...] = ()


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message: _AssistantMessage


class _CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    choices: tuple[_Choice, ...]


@dataclass(frozen=True, slots=True)
class ToolChoice:
    name: str | None
    arguments: dict[str, Any]
    message: str
    call_id: str | None = None


class ToolChoiceProvider(Protocol):
    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice: ...


@dataclass(frozen=True, slots=True)
class GenosToolChoiceProvider:
    base_url: str = field(default_factory=resolve_planner_genos_base_url)
    token: str | None = field(default_factory=resolve_planner_genos_token)
    model: str | None = field(default_factory=lambda: os.environ.get("CHAT_TOOL_USE_MODEL"))
    timeout_s: float = DEFAULT_TOOL_ROUTING_PLANNER_TIMEOUT_S
    max_tokens: int | None = None

    @classmethod
    def from_env(cls) -> GenosToolChoiceProvider:
        return cls()

    @classmethod
    def for_routing_v4(cls) -> GenosToolChoiceProvider:
        return cls(max_tokens=configured_planner_max_tokens())

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        if not self.base_url or not self.token:
            raise ToolProviderConfigurationError("tool planner endpoint or token is not configured")
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "n": 1,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload.update(
                {
                    "tools": tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )
        if self.model:
            payload["model"] = self.model
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        completion = _CompletionResponse.model_validate(response.json())
        if not completion.choices:
            raise ToolProviderConfigurationError("tool planner returned no choices")
        message = completion.choices[0].message
        if not message.tool_calls:
            return ToolChoice(None, {}, str(message.content or ""), call_id=None)
        tool_call = message.tool_calls[0]
        arguments = json.loads(tool_call.function.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ToolProviderConfigurationError("tool arguments must be a JSON object")
        return ToolChoice(
            tool_call.function.name,
            {str(key): value for key, value in arguments.items()},
            str(message.content or user_text),
            call_id=tool_call.id,
        )
