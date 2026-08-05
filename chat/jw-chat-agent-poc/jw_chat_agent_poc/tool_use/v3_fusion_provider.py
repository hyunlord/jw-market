from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
import time
from typing import Any

import requests

from jw_chat_agent_poc.common.token_usage import usage_call_from_payload
from jw_chat_agent_poc.genos_config import (
    resolve_final_genos_base_url,
    resolve_final_genos_token,
)


DEFAULT_FUSION_MAX_TOKENS = 5120


class FusionProviderConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FusionProviderResult:
    content: str
    raw_text: str
    raw_bytes_sha256: str
    raw_response: Mapping[str, object]
    usage: Mapping[str, object] | None
    model: str
    latency_ms: float
    completed_at_utc: str
    request_body_sha256: str
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class GenosV3FusionProvider:
    base_url: str = field(default_factory=resolve_final_genos_base_url)
    token: str | None = field(default_factory=resolve_final_genos_token)
    model: str | None = field(
        default_factory=lambda: os.environ.get("CHAT_V3_FUSION_MODEL")
    )
    timeout_s: float = 60.0
    max_tokens: int = DEFAULT_FUSION_MAX_TOKENS
    temperature: float = 0.0

    @classmethod
    def from_env(cls) -> GenosV3FusionProvider:
        return cls()

    def generate(self, *, messages: list[dict[str, str]]) -> FusionProviderResult:
        if not self.base_url or not self.token:
            raise FusionProviderConfigurationError(
                "fusion endpoint or token is not configured"
            )
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": False,
            "temperature": self.temperature,
            "n": 1,
            "max_tokens": self.max_tokens,
        }
        if self.model:
            payload["model"] = self.model
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        started = time.monotonic()
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        latency_ms = (time.monotonic() - started) * 1000
        raw_bytes = response.content
        raw_text = raw_bytes.decode(response.encoding or "utf-8", errors="replace")
        raw = json.loads(raw_text)
        if not isinstance(raw, Mapping):
            raise FusionProviderConfigurationError(
                "fusion provider returned a non-object response"
            )
        content = _completion_content(raw)
        usage = usage_call_from_payload(raw, base_url=self.base_url, stream=False)
        return FusionProviderResult(
            content=content,
            raw_text=raw_text,
            raw_bytes_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            raw_response=dict(raw),
            usage=usage,
            model=str(raw.get("model") or self.model or ""),
            latency_ms=latency_ms,
            completed_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            request_body_sha256=hashlib.sha256(encoded).hexdigest(),
            finish_reason=_completion_finish_reason(raw),
        )


def _completion_content(payload: Mapping[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise FusionProviderConfigurationError("fusion provider returned no choices")
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise FusionProviderConfigurationError("fusion provider returned no content")
    return content


def _completion_finish_reason(payload: Mapping[str, object]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    finish_reason = first.get("finish_reason") if isinstance(first, Mapping) else None
    return finish_reason if isinstance(finish_reason, str) else None


__all__ = [
    "DEFAULT_FUSION_MAX_TOKENS",
    "FusionProviderConfigurationError",
    "FusionProviderResult",
    "GenosV3FusionProvider",
]
