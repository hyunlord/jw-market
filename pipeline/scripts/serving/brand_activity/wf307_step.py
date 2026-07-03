from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final

JSON_URL: Final = "http://code-serving-238:8080/json"
HTTP_TIMEOUT_SECONDS: Final = 60
MAX_REAL_CALLS_CAP: Final = 250
DEFAULT_MAX_REAL_CALLS: Final = 212
DEFAULT_BRANDS_PER_MARKET: Final = 10000
DEFAULT_LARGE_MARKET_LIMIT: Final = 0


class WorkflowStepError(RuntimeError):
    """Raised when wf307 cannot submit or inspect a topic extraction run."""


async def run(data: dict, opener: Callable | None = None) -> dict:
    """Run wf307's code-serving-238 wrapper step."""
    payload = dict(data or {})
    call = opener or urllib.request.urlopen

    try:
        action = _action(payload)
        match action:
            case "start":
                text_payload = _start_topic(payload, call)
            case "status":
                text_payload = _status(payload, call)
            case "result":
                text_payload = _result(payload, call)
            case unreachable:
                raise WorkflowStepError(f"unknown action={unreachable!r}")

        payload["text"] = json.dumps(text_payload, ensure_ascii=False, sort_keys=True)
        payload["wf307_topic"] = {
            "ok": True,
            "action": text_payload.get("action"),
            "run_id": text_payload.get("run_id"),
            "status": text_payload.get("status"),
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


def _action(data: dict) -> str:
    value = data.get("action", "start")
    if not isinstance(value, str):
        raise WorkflowStepError("action must be a string")
    return value.lower().strip() or "start"


def _start_topic(data: dict, opener: Callable) -> dict:
    arguments = _topic_arguments(data)
    start = _rpc("run_topic_extraction", arguments, "wf307-start", opener)
    start_payload = _tool_payload(start)
    run_id = _string_field(start_payload, "run_id")
    return {
        "action": "start",
        "run_id": run_id,
        "status": start_payload.get("status"),
        "submitted_params": arguments,
        "next": "Call wf307 again with action=status and this run_id.",
    }


def _status(data: dict, opener: Callable) -> dict:
    run_id = _required_run_id(data, "status")
    status_payload = _tool_payload(_rpc("get_status", {"run_id": run_id}, "wf307-status", opener))
    return {
        "action": "status",
        "run_id": run_id,
        "status": status_payload.get("status"),
        "details": status_payload,
    }


def _result(data: dict, opener: Callable) -> dict:
    run_id = _required_run_id(data, "result")
    result_payload = _tool_payload(_rpc("get_result", {"run_id": run_id}, "wf307-result", opener))
    summary = result_payload.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    return {
        "action": "result",
        "run_id": run_id,
        "status": result_payload.get("status"),
        "summary": summary,
        "executed_call_count": summary.get("executed_call_count"),
        "quality_grade_distribution": result_payload.get("quality_grade_distribution"),
        "db_save_summary": result_payload.get("db_save_summary"),
        "zip_sha256": result_payload.get("zip_sha256"),
    }


def _topic_arguments(data: dict) -> dict:
    dry_run = _as_bool(data.get("dry_run"), default=True)
    max_real_calls = _as_int(data.get("max_real_calls"), default=DEFAULT_MAX_REAL_CALLS)
    if max_real_calls < 0:
        raise WorkflowStepError("max_real_calls must be >= 0")
    if max_real_calls > MAX_REAL_CALLS_CAP:
        raise WorkflowStepError(f"max_real_calls must be <= {MAX_REAL_CALLS_CAP}")
    arguments = {
        "dry_run": dry_run,
        "save_to_db": False if dry_run else _as_bool(data.get("save_to_db"), default=True),
        "max_real_calls": max_real_calls,
        "brands_per_market": _as_int(data.get("brands_per_market"), default=DEFAULT_BRANDS_PER_MARKET),
        "large_market_limit": _as_int(data.get("large_market_limit"), default=DEFAULT_LARGE_MARKET_LIMIT),
        "stage_schema": "jw_brand_activity_stage",
        "raw_schema": "jw_brand_activity_raw_stage",
    }
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


def _required_run_id(data: dict, action: str) -> str:
    value = data.get("run_id")
    if not isinstance(value, str) or not value.strip():
        raise WorkflowStepError(f"run_id is required for action={action}")
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
