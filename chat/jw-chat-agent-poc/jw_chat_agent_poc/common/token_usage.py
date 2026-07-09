from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


def usage_call_from_payload(payload: Mapping[str, Any], *, base_url: str, stream: bool) -> dict[str, Any] | None:
    usage = payload.get("usage") or payload.get("usage_metadata") or payload.get("usageMetadata")
    if not isinstance(usage, Mapping):
        return None
    input_tokens = _int_from_keys(usage, ("prompt_tokens", "input_tokens", "prompt_token_count"))
    output_tokens = _int_from_keys(
        usage,
        ("completion_tokens", "output_tokens", "candidates_token_count", "candidate_tokens"),
    )
    total_tokens = _int_from_keys(usage, ("total_tokens", "total_token_count"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return {
        "model": _model_name(payload),
        "serving_id": _serving_id(base_url),
        "stream": stream,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "raw_usage": dict(usage),
    }


def record_token_usage(timing: dict[str, Any] | None, call: Mapping[str, Any] | None) -> None:
    if timing is None or call is None:
        return
    usage = timing.setdefault("token_usage", {"available": False, "calls": []})
    if not isinstance(usage, dict):
        usage = {"available": False, "calls": []}
        timing["token_usage"] = usage
    calls = usage.setdefault("calls", [])
    if not isinstance(calls, list):
        calls = []
        usage["calls"] = calls
    call_payload = dict(call)
    calls.append(call_payload)
    usage["available"] = True
    usage["total_input_tokens"] = _sum_tokens(calls, "input_tokens")
    usage["total_output_tokens"] = _sum_tokens(calls, "output_tokens")
    usage["total_tokens"] = _sum_tokens(calls, "total_tokens")


def public_token_usage(timing: Mapping[str, Any]) -> dict[str, Any]:
    usage = timing.get("token_usage")
    if not isinstance(usage, Mapping) or not usage.get("available"):
        return {"available": False, "calls": [], "total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0}
    calls = usage.get("calls") if isinstance(usage.get("calls"), list) else []
    return {
        "available": bool(calls),
        "calls": calls,
        "total_input_tokens": int(usage.get("total_input_tokens") or 0),
        "total_output_tokens": int(usage.get("total_output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _int_from_keys(payload: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _sum_tokens(calls: list[Any], key: str) -> int:
    total = 0
    for call in calls:
        if isinstance(call, Mapping) and isinstance(call.get(key), int):
            total += int(call[key])
    return total


def _serving_id(base_url: str) -> str:
    path = urlparse(base_url).path.rstrip("/")
    marker = "/serving/"
    if marker not in path:
        return ""
    return path.rsplit(marker, maxsplit=1)[-1].split("/", maxsplit=1)[0]


def _model_name(payload: Mapping[str, Any]) -> str:
    model = payload.get("model")
    return model if isinstance(model, str) else ""
