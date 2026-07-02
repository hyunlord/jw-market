from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

import anyio

from pipeline.scripts.serving.brand_activity import wf307_step


@dataclass(frozen=True, slots=True)
class CapturedRequest:
    """One JSON-RPC request captured from the wf307 wrapper."""

    method: str
    arguments: dict


class FakeResponse:
    """Context manager matching urllib response enough for wf307_step tests."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


def test_wf307_step_starts_dry_run_and_returns_result(monkeypatch) -> None:
    """Given safe dry-run input, When wf307 runs, Then it polls 238 and returns the result text."""
    captured: list[CapturedRequest] = []

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        body = json.loads(request.data.decode("utf-8"))
        params = body["params"]
        captured.append(CapturedRequest(method=params["name"], arguments=params["arguments"]))
        return FakeResponse(_response_for(params["name"]))

    monkeypatch.setattr(wf307_step.time, "sleep", lambda seconds: None)

    result = anyio.run(
        wf307_step.run,
        {"dry_run": True, "save_to_db": True},
        fake_urlopen,
    )

    text = json.loads(result["text"])
    assert text["run_id"] == "topic_20260702_test"
    assert text["status"] == "done"
    assert text["result"]["summary"]["execution_mode"] == "dry_run"
    assert text["result"]["summary"]["executed_call_count"] == 0
    assert captured[0] == CapturedRequest(
        method="run_topic_extraction",
        arguments={
            "dry_run": True,
            "save_to_db": False,
            "max_real_calls": 0,
            "brands_per_market": 1,
            "large_market_limit": 0,
            "stage_schema": "jw_brand_activity_stage",
            "raw_schema": "jw_brand_activity_raw_stage",
        },
    )
    assert [request.method for request in captured] == [
        "run_topic_extraction",
        "get_status",
        "get_result",
    ]


def test_wf307_step_returns_diagnostic_text_on_rpc_error() -> None:
    """Given a JSON-RPC error, When wf307 runs, Then it returns a diagnostic instead of crashing."""

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        return FakeResponse(
            {
                "jsonrpc": "2.0",
                "id": "wf307-start",
                "error": {"code": -32000, "message": "dry-run unavailable"},
            }
        )

    result = anyio.run(wf307_step.run, {}, fake_urlopen)

    text = json.loads(result["text"])
    assert text["ok"] is False
    assert text["error_type"] == "WorkflowStepError"
    assert result["wf307_topic"] == {"ok": False, "error_type": "WorkflowStepError"}


def _response_for(tool_name: str) -> dict:
    match tool_name:
        case "run_topic_extraction":
            payload: Final = {"run_id": "topic_20260702_test", "status": "started"}
        case "get_status":
            payload = {"run_id": "topic_20260702_test", "status": "done"}
        case "get_result":
            payload = {
                "run_id": "topic_20260702_test",
                "status": "done",
                "summary": {"execution_mode": "dry_run", "executed_call_count": 0},
            }
        case unreachable:
            raise AssertionError(f"unexpected tool: {unreachable}")
    return {
        "jsonrpc": "2.0",
        "id": "wf307-test",
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload,
            "isError": False,
        },
    }
