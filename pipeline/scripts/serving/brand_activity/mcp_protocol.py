from __future__ import annotations

import json
from typing import Final

from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue
from pipeline.scripts.serving.brand_activity.topic_jobs import RunId, TopicJobError, TopicJobStore, parse_topic_request

JSONRPC_VERSION: Final = "2.0"


def handle_jsonrpc(request: dict[str, JsonValue], store: TopicJobStore) -> dict[str, JsonValue]:
    """Handle one JSON-RPC request for the topic child server."""
    request_id = request.get("id")
    method = request.get("method")
    if method is None:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": {"status": "ok"}}
    if method == "tools/list":
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": {"tools": _tools()}}
    if method == "tools/call":
        return _tool_call_response(request_id, request.get("params"), store)
    return _error_response(request_id, -32601, f"unknown method: {method}")


def _tool_call_response(request_id: JsonValue, params: JsonValue, store: TopicJobStore) -> dict[str, JsonValue]:
    if not isinstance(params, dict):
        return _error_response(request_id, -32602, "tools/call params must be an object")
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(name, str):
        return _error_response(request_id, -32602, "tools/call params.name must be a string")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error_response(request_id, -32602, "tools/call params.arguments must be an object")
    try:
        if name == "run_topic_extraction":
            request = parse_topic_request(arguments)
            run_id = store.start(request)
            payload: dict[str, JsonValue] = {"run_id": run_id, "status": "started"}
        elif name == "get_status":
            payload = store.status(RunId(_run_id(arguments)))
        elif name == "get_result":
            payload = store.result(RunId(_run_id(arguments)))
        else:
            return _error_response(request_id, -32601, f"unknown tool: {name}")
    except (TopicJobError, TypeError, ValueError) as exc:
        return _error_response(request_id, -32000, str(exc))
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": _mcp_content(payload)}


def _run_id(arguments: dict[str, JsonValue]) -> str:
    run_id = arguments.get("run_id")
    if not isinstance(run_id, str):
        raise TopicJobError("run_id is required")
    return run_id


def _mcp_content(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
        "structuredContent": payload,
        "isError": False,
    }


def _error_response(request_id: JsonValue, code: int, message: str) -> dict[str, JsonValue]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": {"code": code, "message": message}}


def _tools() -> list[dict[str, JsonValue]]:
    return [
        {
            "name": "run_topic_extraction",
            "description": "Start Brand Activity topic extraction via brand_activity_replay --only topic.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "default": True},
                    "save_to_db": {"type": "boolean", "default": False},
                    "max_real_calls": {"type": "integer", "default": 86},
                    "brands_per_market": {"type": "integer", "minimum": 1},
                    "brand_rows": {"type": "integer", "default": 5},
                    "axis_per_brand": {"type": "integer", "default": 3},
                    "large_market_limit": {"type": "integer", "default": 0},
                    "stage_schema": {"type": "string", "default": "jw_brand_activity_stage"},
                    "raw_schema": {"type": "string", "default": "jw_brand_activity_raw_stage"},
                },
            },
        },
        {
            "name": "get_status",
            "description": "Read the status of a previously started topic extraction run.",
            "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
        },
        {
            "name": "get_result",
            "description": "Read the final result of a topic extraction run.",
            "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
        },
    ]
