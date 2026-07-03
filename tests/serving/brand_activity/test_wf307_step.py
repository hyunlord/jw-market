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


def test_wf307_step_start_returns_run_id_without_polling() -> None:
    """Given safe dry-run input, When wf307 starts, Then it returns the run id without polling."""
    captured: list[CapturedRequest] = []

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        body = json.loads(request.data.decode("utf-8"))
        params = body["params"]
        captured.append(CapturedRequest(method=params["name"], arguments=params["arguments"]))
        return FakeResponse(_response_for(params["name"]))

    result = anyio.run(
        wf307_step.run,
        {"action": "start", "dry_run": True, "save_to_db": True, "max_real_calls": 0},
        fake_urlopen,
    )

    text = json.loads(result["text"])
    assert text["action"] == "start"
    assert text["run_id"] == "topic_20260702_test"
    assert text["submitted_params"]["save_to_db"] is False
    assert text["next"] == "Call wf307 again with action=status and this run_id."
    assert captured[0] == CapturedRequest(
        method="run_topic_extraction",
        arguments={
            "dry_run": True,
            "save_to_db": False,
            "max_real_calls": 0,
            "brands_per_market": 10000,
            "large_market_limit": 0,
            "stage_schema": "jw_brand_activity_stage",
            "raw_schema": "jw_brand_activity_raw_stage",
        },
    )
    assert [request.method for request in captured] == ["run_topic_extraction"]


def test_wf307_step_status_requires_run_id_and_calls_status_tool() -> None:
    """Given a run id, When wf307 checks status, Then it returns the current status payload."""
    captured: list[CapturedRequest] = []

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        body = json.loads(request.data.decode("utf-8"))
        params = body["params"]
        captured.append(CapturedRequest(method=params["name"], arguments=params["arguments"]))
        return FakeResponse(_response_for(params["name"]))

    result = anyio.run(wf307_step.run, {"action": "status", "run_id": "topic_20260702_test"}, fake_urlopen)

    text = json.loads(result["text"])
    assert text["action"] == "status"
    assert text["run_id"] == "topic_20260702_test"
    assert text["status"] == "done"
    assert captured == [
        CapturedRequest(method="get_status", arguments={"run_id": "topic_20260702_test"})
    ]


def test_wf307_step_result_returns_summary() -> None:
    """Given a run id, When wf307 fetches result, Then it returns a compact topic summary."""
    captured: list[CapturedRequest] = []

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        body = json.loads(request.data.decode("utf-8"))
        params = body["params"]
        captured.append(CapturedRequest(method=params["name"], arguments=params["arguments"]))
        return FakeResponse(_response_for(params["name"]))

    result = anyio.run(wf307_step.run, {"action": "result", "run_id": "topic_20260702_test"}, fake_urlopen)

    text = json.loads(result["text"])
    assert text == {
        "action": "result",
        "db_save_summary": {"stored_run_rows": 0, "stored_topic_rows": 0},
        "executed_call_count": 0,
        "quality_grade_distribution": {"A": 11},
        "run_id": "topic_20260702_test",
        "status": "done",
        "summary": {"executed_call_count": 0, "execution_mode": "dry_run"},
        "zip_sha256": "sha-test",
    }
    assert captured == [
        CapturedRequest(method="get_result", arguments={"run_id": "topic_20260702_test"})
    ]


def test_wf307_step_rejects_call_cap_excess() -> None:
    """Given an excessive call cap, When wf307 starts, Then it returns an error before RPC."""

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        raise AssertionError("start must reject before making an RPC request")

    result = anyio.run(wf307_step.run, {"action": "start", "max_real_calls": 9999}, fake_urlopen)

    text = json.loads(result["text"])
    assert text["ok"] is False
    assert text["error_type"] == "WorkflowStepError"
    assert "max_real_calls must be <= 250" in text["error"]


def test_wf307_step_requires_run_id_for_status() -> None:
    """Given no run id, When wf307 checks status, Then it returns a clear diagnostic."""

    def fake_urlopen(request, timeout: int) -> FakeResponse:
        raise AssertionError("status must reject before making an RPC request")

    result = anyio.run(wf307_step.run, {"action": "status"}, fake_urlopen)

    text = json.loads(result["text"])
    assert text["ok"] is False
    assert text["error_type"] == "WorkflowStepError"
    assert text["error"] == "run_id is required for action=status"


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
                "db_save_summary": {"stored_run_rows": 0, "stored_topic_rows": 0},
                "quality_grade_distribution": {"A": 11},
                "zip_sha256": "sha-test",
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
