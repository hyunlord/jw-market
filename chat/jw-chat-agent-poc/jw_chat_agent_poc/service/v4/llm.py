from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
import json
import logging
import os
import re
import time

import requests

from jw_chat_agent_poc.genos_config import (
    resolve_final_genos_base_url,
    resolve_final_genos_token,
    resolve_planner_genos_base_url,
    resolve_planner_genos_token,
)
from jw_chat_agent_poc.service.genos_client import GenosClient


LOGGER = logging.getLogger(__name__)

PLANNER_MODEL = "gemini-3-flash-preview"
SYNTHESIZER_MODEL = "gemini-3.1-pro-preview"
_THINKING_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})


@dataclass(frozen=True)
class CompletionResult:
    text: str
    finish_reason: str | None
    usage: dict[str, object]
    elapsed_ms: float
    serving_id: str = "unknown"
    model: str = "unknown"


class GenOSV4Client:
    """Small GenOS serving facade that bypasses the legacy finalizer."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        model: str,
        timeout_s: int,
        total_budget_s: int,
        thinking_level: str | None = None,
    ) -> None:
        self._client = GenosClient(
            base_url=base_url,
            token=token,
            timeout_s=timeout_s,
            total_budget_s=total_budget_s,
            model=model,
        )
        normalized = thinking_level.strip().upper() if thinking_level else None
        if normalized is not None and normalized not in _THINKING_LEVELS:
            raise ValueError(f"unsupported thinking_level: {thinking_level}")
        self._thinking_level = normalized

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        budget_s: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        client = self._client
        if budget_s is not None:
            bounded = max(1, int(budget_s))
            client = replace(
                client,
                timeout_s=min(client.timeout_s, bounded),
                total_budget_s=min(client.total_budget_s, bounded),
            )
        return _chat_completion_with_token_cap(
            client,
            list(messages),
            max_tokens=max_tokens,
            thinking_level=self._thinking_level,
        ).text

    def complete_detailed(
        self,
        messages: Sequence[dict[str, str]],
        *,
        budget_s: float | None = None,
        max_tokens: int,
    ) -> CompletionResult:
        client = self._client
        if budget_s is not None:
            bounded = max(1, int(budget_s))
            client = replace(
                client,
                timeout_s=min(client.timeout_s, bounded),
                total_budget_s=min(client.total_budget_s, bounded),
            )
        return _chat_completion_with_token_cap(
            client,
            list(messages),
            max_tokens=max_tokens,
            thinking_level=self._thinking_level,
        )

    @property
    def serving_id(self) -> str:
        marker = "/serving/"
        base_url = self._client.base_url.rstrip("/")
        return base_url.rsplit(marker, 1)[-1].split("/", 1)[0] if marker in base_url else "unknown"

    @property
    def thinking_level(self) -> str | None:
        return self._thinking_level


def planner_client() -> GenOSV4Client:
    return GenOSV4Client(
        base_url=resolve_planner_genos_base_url(),
        token=resolve_planner_genos_token(),
        model=os.environ.get("V4_PLANNER_MODEL", PLANNER_MODEL),
        timeout_s=int(os.environ.get("V4_PLANNER_TIMEOUT_S", "18")),
        total_budget_s=int(os.environ.get("V4_PLANNER_BUDGET_S", "24")),
        thinking_level=os.environ.get("V4_PLANNER_THINKING_LEVEL", "LOW"),
    )


def synthesizer_client() -> GenOSV4Client:
    serving_id = os.environ.get("GENOS_SYNTH_SERVING_ID")
    if serving_id is None:
        serving_id = os.environ.get("V4_SYNTHESIZER_SERVING_ID", "202")
        LOGGER.warning(
            "GENOS_SYNTH_SERVING_ID is unset; using fail-safe serving %s",
            serving_id,
        )
    base_url = re.sub(
        r"/serving/\d+(?=/|$)",
        f"/serving/{serving_id}",
        resolve_final_genos_base_url(),
    )
    return GenOSV4Client(
        base_url=base_url,
        token=(
            os.environ.get("GENOS_SYNTH_BEARER_TOKEN")
            or os.environ.get("V4_SYNTHESIZER_BEARER_TOKEN")
            or resolve_final_genos_token()
        ),
        model=os.environ.get(
            "GENOS_SYNTH_MODEL",
            os.environ.get("V4_SYNTHESIZER_MODEL", SYNTHESIZER_MODEL),
        ),
        timeout_s=int(os.environ.get("V4_SYNTHESIZER_TIMEOUT_S", "60")),
        total_budget_s=int(os.environ.get("V4_SYNTHESIZER_BUDGET_S", "64")),
        thinking_level=os.environ.get("V4_SYNTHESIZER_THINKING_LEVEL", "MEDIUM"),
    )


def _chat_completion_with_token_cap(
    client: GenosClient,
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None,
    thinking_level: str | None = None,
) -> CompletionResult:
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    started = time.monotonic()
    payload = {
        "messages": messages,
        "stream": True,
        "temperature": 0.0,
        "stream_options": {"include_usage": True},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if thinking_level is not None:
        payload["google"] = {
            "thinking_config": {"thinking_level": thinking_level}
        }
    if client.model:
        payload["model"] = client.model
    headers = {"Authorization": f"Bearer {client.token}"} if client.token else {}
    response = requests.post(
        f"{client.base_url.rstrip('/')}/chat/completions",
        headers=headers,
        json=payload,
        stream=True,
        timeout=float(client.timeout_s),
    )
    response.raise_for_status()
    chunks: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, object] = {}
    serving_id = "unknown"
    model = "unknown"
    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            if time.monotonic() - started >= client.total_budget_s:
                raise requests.Timeout("V4 synthesis budget exceeded")
            if not raw_line or not raw_line.startswith("data:"):
                continue
            encoded = raw_line.removeprefix("data:").strip()
            if encoded == "[DONE]":
                break
            try:
                data = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            if isinstance(data.get("usage"), dict):
                usage = dict(data["usage"])
            response_model = data.get("model")
            if isinstance(response_model, str) and response_model:
                serving_id, model = _response_model_identity(response_model)
            for choice in data.get("choices") or ():
                if isinstance(choice, dict) and choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
            token = client._extract_delta_from_data(data)  # noqa: SLF001 - transport-compatible parser
            if token:
                chunks.append(token)
    finally:
        response.close()
    return CompletionResult(
        text="".join(chunks).strip(),
        finish_reason=finish_reason,
        usage=usage,
        elapsed_ms=(time.monotonic() - started) * 1000,
        serving_id=serving_id,
        model=model,
    )


def _response_model_identity(response_model: str) -> tuple[str, str]:
    match = re.fullmatch(r"genos/([^/]+)/(.+)", response_model.strip())
    if match is None:
        return "unknown", response_model.strip() or "unknown"
    return match.group(1), match.group(2)
