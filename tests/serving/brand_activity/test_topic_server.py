from __future__ import annotations

import json
from pathlib import Path

from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue
from pipeline.scripts.etl.brand_activity import brand_activity_replay as replay
from pipeline.scripts.serving.brand_activity import mcp_protocol
from pipeline.scripts.serving.brand_activity.topic_jobs import TopicJobStore


def test_mcp_tools_list_exposes_topic_runner_tools() -> None:
    """Given a topic server store, When tools/list is called, Then the three runner tools are exposed."""
    store = TopicJobStore()

    response = mcp_protocol.handle_jsonrpc(
        {"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        store,
    )

    assert response["jsonrpc"] == "2.0"
    result = response["result"]
    assert isinstance(result, dict)
    tools = result["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == [
        "run_topic_extraction",
        "get_status",
        "get_result",
    ]


def test_topic_job_dry_run_returns_run_id_then_result(monkeypatch, tmp_path: Path) -> None:
    """Given dry-run arguments, When the MCP tool starts a job, Then status/result are available by run_id."""
    store = TopicJobStore()
    calls: list[tuple[bool, bool, int]] = []

    def fake_replay(options: replay.ReplayOptions) -> dict[str, JsonValue]:
        calls.append((options.execute, options.save_to_db, options.topic.max_real_calls))
        return {"plan": ["topic"], "results": {"topic": {"stage": "topic", "execute": options.execute}}}

    monkeypatch.setenv("BRAND_ACTIVITY_TOPIC_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr("pipeline.scripts.serving.brand_activity.topic_jobs.replay", fake_replay)

    start = mcp_protocol.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 2,
            "params": {
                "name": "run_topic_extraction",
                "arguments": {"dry_run": True, "max_real_calls": 4, "save_to_db": False},
            },
        },
        store,
    )
    payload = _tool_payload(start)

    assert payload["status"] == "started"
    run_id = payload["run_id"]
    assert isinstance(run_id, str)

    status = _tool_payload(
        mcp_protocol.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 3,
                "params": {"name": "get_status", "arguments": {"run_id": run_id}},
            },
            store,
        )
    )
    result = _tool_payload(
        mcp_protocol.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 4,
                "params": {"name": "get_result", "arguments": {"run_id": run_id}},
            },
            store,
        )
    )

    assert status["status"] == "done"
    assert result["status"] == "done"
    assert result["summary"] == {"plan": ["topic"], "results": {"topic": {"stage": "topic", "execute": False}}}
    assert calls == [(False, False, 4)]


def _tool_payload(response: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return the JSON payload embedded in an MCP text content response."""
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    first = content[0]
    assert isinstance(first, dict)
    text = first["text"]
    assert isinstance(text, str)
    loaded = json.loads(text)
    assert isinstance(loaded, dict)
    return loaded
