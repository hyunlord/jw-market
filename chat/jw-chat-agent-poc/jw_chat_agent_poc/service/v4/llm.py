from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import json
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


PLANNER_MODEL = "gemini-3.1-pro-preview"
SYNTHESIZER_MODEL = "gemini-3-flash-preview"


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
    ) -> None:
        self._client = GenosClient(
            base_url=base_url,
            token=token,
            timeout_s=timeout_s,
            total_budget_s=total_budget_s,
            model=model,
        )

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
        if max_tokens is None:
            return client._chat_text(list(messages))  # noqa: SLF001 - direct GenOS transport reuse
        return _chat_text_with_token_cap(client, list(messages), max_tokens=max_tokens)

    @property
    def serving_id(self) -> str:
        marker = "/serving/"
        base_url = self._client.base_url.rstrip("/")
        return base_url.rsplit(marker, 1)[-1].split("/", 1)[0] if marker in base_url else "unknown"


def planner_client() -> GenOSV4Client:
    return GenOSV4Client(
        base_url=resolve_planner_genos_base_url(),
        token=resolve_planner_genos_token(),
        model=os.environ.get("V4_PLANNER_MODEL", PLANNER_MODEL),
        timeout_s=int(os.environ.get("V4_PLANNER_TIMEOUT_S", "18")),
        total_budget_s=int(os.environ.get("V4_PLANNER_BUDGET_S", "24")),
    )


def synthesizer_client() -> GenOSV4Client:
    serving_id = os.environ.get("V4_SYNTHESIZER_SERVING_ID", "190")
    base_url = re.sub(
        r"/serving/\d+(?=/|$)",
        f"/serving/{serving_id}",
        resolve_final_genos_base_url(),
    )
    return GenOSV4Client(
        base_url=base_url,
        token=(
            os.environ.get("V4_SYNTHESIZER_BEARER_TOKEN")
            or resolve_final_genos_token()
        ),
        model=os.environ.get("V4_SYNTHESIZER_MODEL", SYNTHESIZER_MODEL),
        timeout_s=int(os.environ.get("V4_SYNTHESIZER_TIMEOUT_S", "15")),
        total_budget_s=int(os.environ.get("V4_SYNTHESIZER_BUDGET_S", "20")),
    )


def _chat_text_with_token_cap(
    client: GenosClient,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
) -> str:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    started = time.monotonic()
    payload = {
        "messages": messages,
        "stream": True,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
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
            token = client._extract_delta_from_data(data)  # noqa: SLF001 - transport-compatible parser
            if token:
                chunks.append(token)
    finally:
        response.close()
    return "".join(chunks).strip()
