from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final

JSON_URL: Final = "http://code-serving-238:8080/json"
HTTP_TIMEOUT_SECONDS: Final = 60
MAX_WAIT_SECONDS: Final = 900
POLL_INTERVAL_SECONDS: Final = 10


class WorkflowStepError(RuntimeError):
    """Raised when wf307 cannot complete the dry-run wrapper flow."""


async def run(data: dict, opener: Callable | None = None) -> dict:
    """Run wf307's code-serving-238 wrapper step."""
    payload = dict(data or {})
    call = opener or urllib.request.urlopen

    try:
        start = _rpc(
            "run_topic_extraction",
            _topic_arguments(payload),
            "wf307-start",
            call,
        )
        run_id = _string_field(_tool_payload(start), "run_id")
        status_payload = _poll_until_finished(run_id, call)
        result_payload = _tool_payload(_rpc("get_result", {"run_id": run_id}, "wf307-result", call))

        payload["text"] = json.dumps(
            {
                "run_id": run_id,
                "status": status_payload.get("status"),
                "result": result_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        payload["wf307_topic"] = {
            "ok": True,
            "run_id": run_id,
            "status": status_payload.get("status"),
        }
        return payload
    except (
        WorkflowStepError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        payload["text"] = json.dumps(
            {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]},
            ensure_ascii=False,
            sort_keys=True,
        )
        payload["wf307_topic"] = {"ok": False, "error_type": type(exc).__name__}
        return payload


def _topic_arguments(data: dict) -> dict:
    dry_run = _as_bool(data.get("dry_run"), default=True)
    arguments = {
        "dry_run": dry_run,
        "save_to_db": False if dry_run else _as_bool(data.get("save_to_db"), default=False),
        "max_real_calls": _as_int(data.get("max_real_calls"), default=0 if dry_run else 86),
        "large_market_limit": _as_int(data.get("large_market_limit"), default=0),
        "stage_schema": "jw_brand_activity_stage",
        "raw_schema": "jw_brand_activity_raw_stage",
    }
    if data.get("brands_per_market") is not None:
        arguments["brands_per_market"] = _as_int(data.get("brands_per_market"), default=0)
    return arguments


def _rpc(name: str, arguments: dict, rpc_id: str, opener: Callable) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        JSON_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        loaded = json.loads(response.read().decode("utf-8"))
    if not isinstance(loaded, dict):
        raise WorkflowStepError("JSON-RPC response must be an object")
    if "error" in loaded:
        raise WorkflowStepError(json.dumps(loaded["error"], ensure_ascii=False, sort_keys=True))
    return loaded


def _poll_until_finished(run_id: str, opener: Callable) -> dict:
    waited = 0
    last_status: dict | None = None
    while waited <= MAX_WAIT_SECONDS:
        last_status = _tool_payload(_rpc("get_status", {"run_id": run_id}, "wf307-status", opener))
        status = last_status.get("status")
        if status in {"done", "error", "failed"}:
            return last_status
        time.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    raise WorkflowStepError(
        f"topic extraction timed out after {MAX_WAIT_SECONDS}s; last_status={last_status}"
    )


def _tool_payload(response: dict) -> dict:
    result = response.get("result")
    if not isinstance(result, dict):
        raise WorkflowStepError("JSON-RPC result must be an object")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return loaded
    raise WorkflowStepError("MCP tool response did not include a JSON object payload")


def _string_field(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise WorkflowStepError(f"{key} must be a string")
    return value


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _as_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise WorkflowStepError(f"expected integer-compatible value, got {type(value).__name__}")
