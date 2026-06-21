from __future__ import annotations

from dataclasses import dataclass
import json
import socket
import time
from typing import TypedDict

import httpx2

from .models import JsonValue


class GenosUsage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class GenosCall(TypedDict):
    status: str
    serving_id: str
    latency_ms: int
    ttfb_ms: int
    read_ms: int
    phase: str
    content: str
    usage: GenosUsage
    error_type: str
    error_message: str


@dataclass(frozen=True, slots=True)
class GenosServingClient:
    base_url: str
    token: str
    serving_id: str
    timeout_s: float = 90.0
    connect_timeout_s: float = 10.0

    def chat(self, messages: list[dict[str, str]]) -> GenosCall:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload: dict[str, JsonValue] = {"messages": messages, "stream": False, "temperature": 0.0}
        start = time.perf_counter()
        phase = "connect"
        ttfb_ms = 0
        read_ms = 0
        try:
            with _client(self.base_url, self.timeout_s, self.connect_timeout_s, headers) as client:
                with client.stream("POST", f"/api/gateway/rep/serving/{self.serving_id}/chat/completions", json=payload) as response:
                    ttfb_ms = int((time.perf_counter() - start) * 1000)
                    phase = "ttfb"
                    response.raise_for_status()
                    read_start = time.perf_counter()
                    content = response.read()
                    read_ms = int((time.perf_counter() - read_start) * 1000)
                    phase = "read"
                    body = json.loads(content)
        except (httpx2.HTTPError, json.JSONDecodeError) as exc:
            return {
                "status": "error",
                "serving_id": self.serving_id,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "ttfb_ms": ttfb_ms,
                "read_ms": read_ms,
                "phase": phase,
                "content": "",
                "usage": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
        return {
            "status": "ok",
            "serving_id": self.serving_id,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "ttfb_ms": ttfb_ms,
            "read_ms": read_ms,
            "phase": "complete",
            "content": _extract_content(body),
            "usage": _extract_usage(body),
            "error_type": "",
            "error_message": "",
        }


def parse_json_object(content: str) -> dict[str, JsonValue]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return {"_invalid": "no_json_object", "raw_length": len(content)}
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        return {"_invalid": "json_decode_error", "message": str(exc), "raw_length": len(content)}
    if isinstance(data, dict):
        return data
    return {"_invalid": "not_object", "raw_length": len(content)}


def _client(base_url: str, timeout_s: float, connect_timeout_s: float, headers: dict[str, str]) -> httpx2.Client:
    limits = httpx2.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)
    timeout = httpx2.Timeout(connect=connect_timeout_s, read=timeout_s, write=10.0, pool=10.0)
    transport = httpx2.HTTPTransport(
        http2=True,
        retries=3,
        limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
    )
    return httpx2.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        transport=transport,
        follow_redirects=True,
    )


def _extract_content(payload: JsonValue) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            text = first.get("text")
            if isinstance(text, str):
                return text
    content = payload.get("content")
    if isinstance(content, str):
        return content
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return data["content"]
    return ""


def _extract_usage(payload: JsonValue) -> GenosUsage:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    result: GenosUsage = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            result[key] = value
    return result
