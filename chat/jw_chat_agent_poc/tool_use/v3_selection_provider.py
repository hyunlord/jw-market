from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any

from pydantic import BaseModel, ConfigDict
import requests

from jw_chat_agent_poc.genos_config import (
    resolve_planner_genos_base_url,
    resolve_planner_genos_token,
)
from jw_chat_agent_poc.tool_use.v3_selection import MultiToolChoice


class V3ToolProviderConfigurationError(RuntimeError):
    """Raised when the selection-only planner cannot satisfy its contract."""


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

    tool_calls: tuple[_ProviderToolCall, ...] = ()


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message: _AssistantMessage


class _CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    choices: tuple[_Choice, ...]


@dataclass(frozen=True, slots=True)
class GenosV3ToolChoiceProvider:
    base_url: str = field(default_factory=resolve_planner_genos_base_url)
    token: str | None = field(default_factory=resolve_planner_genos_token)
    model: str | None = field(default_factory=lambda: os.environ.get("CHAT_TOOL_USE_MODEL"))
    timeout_s: float = 20.0
    max_tokens: int = 1024

    @classmethod
    def from_env(cls) -> GenosV3ToolChoiceProvider:
        return cls()

    def choose_many(
        self,
        *,
        user_text: str,
        messages: list[dict],
        tools: list[dict],
    ) -> tuple[MultiToolChoice, ...]:
        del user_text
        if not self.base_url or not self.token:
            raise V3ToolProviderConfigurationError(
                "tool planner endpoint or token is not configured"
            )
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "n": 1,
            "max_tokens": self.max_tokens,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
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
            raise V3ToolProviderConfigurationError("tool planner returned no choices")
        selected: list[MultiToolChoice] = []
        for tool_call in completion.choices[0].message.tool_calls:
            arguments = json.loads(tool_call.function.arguments or "{}")
            if not isinstance(arguments, dict):
                raise V3ToolProviderConfigurationError(
                    "tool arguments must be a JSON object"
                )
            selected.append(
                MultiToolChoice(
                    name=tool_call.function.name,
                    arguments={str(key): value for key, value in arguments.items()},
                    call_id=tool_call.id,
                )
            )
        return tuple(selected)
