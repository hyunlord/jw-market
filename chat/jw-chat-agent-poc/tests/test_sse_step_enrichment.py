from __future__ import annotations

import json
import time

from jw_chat_agent_poc.common import timing
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore, _stream_resolving_session_events


def _step_payloads(events: list[str]) -> list[dict]:
    return [json.loads(event.split("data: ", 1)[1]) for event in events if event.startswith("event: step\n")]


def test_stage_heartbeat_is_additive_and_keeps_merge_key(monkeypatch) -> None:
    monkeypatch.setenv(timing.STEP_HEARTBEAT_THRESHOLD_S_ENV, "0.01")
    monkeypatch.setattr(timing, "STEP_HEARTBEAT_INTERVAL_S", 0.01)
    events: list[dict] = []
    with timing.stage(None, "llm_plan", "safe detail", sink=events.append) as progress:
        progress.summary = "get_brand_metric"
        time.sleep(0.04)

    assert events[0]["status"] == "started"
    assert events[-1]["status"] == "done"
    assert any(event["status"] == "in_progress" for event in events)
    assert {(event["raw_name"], event["detail"]) for event in events} == {("llm_plan", "safe detail")}
    assert events[-1]["summary"] == "get_brand_metric"
    assert set(events[0]).issubset(events[-1])


def test_heartbeat_sink_failure_does_not_affect_stage(monkeypatch) -> None:
    monkeypatch.setenv(timing.STEP_HEARTBEAT_THRESHOLD_S_ENV, "0.01")

    def sink(event: dict) -> None:
        if event["status"] == "in_progress":
            raise RuntimeError("presentation failure")

    with timing.stage(None, "llm_plan", "safe detail", sink=sink):
        time.sleep(0.03)


def test_short_stage_has_no_heartbeat(monkeypatch) -> None:
    monkeypatch.setenv(timing.STEP_HEARTBEAT_THRESHOLD_S_ENV, "0.05")
    events: list[dict] = []
    with timing.stage(None, "llm_plan", "short", sink=events.append):
        pass
    time.sleep(0.06)
    assert [event["status"] for event in events] == ["started", "done"]


def test_invalid_heartbeat_threshold_falls_back(monkeypatch) -> None:
    monkeypatch.setenv(timing.STEP_HEARTBEAT_THRESHOLD_S_ENV, "not-a-number")
    events: list[dict] = []
    with timing.stage(None, "llm_plan", "safe", sink=events.append):
        pass
    assert [event["status"] for event in events] == ["started", "done"]


class _WaitingLimiter:
    def __init__(self) -> None:
        self.released = False

    def try_acquire(self) -> bool:
        time.sleep(0.04)
        return False

    def release(self) -> None:
        self.released = True


def test_stream_reports_semaphore_wait_before_busy(monkeypatch) -> None:
    monkeypatch.setattr(service_app, "QUEUE_PROGRESS_THRESHOLD_S", 0.01)
    monkeypatch.setattr(service_app, "QUEUE_PROGRESS_INTERVAL_S", 0.01)
    limiter = _WaitingLimiter()
    events = list(_stream_resolving_session_events(SessionStore(), object(), object(), "question", "live", None, limiter=limiter))
    steps = _step_payloads(events)
    assert steps
    assert steps[0]["raw_name"] == "queue_wait"
    assert steps[0]["status"] == "in_progress"
    assert limiter.released is False
    assert events[-1].rstrip().endswith("data: error")


def test_question_received_is_first_for_immediate_slot(monkeypatch) -> None:
    def answer_question(*args, **kwargs):
        return {"question": "q", "result": {"answer": "ok", "sources": [], "tool_calls": []}, "conversation_id": "c"}

    monkeypatch.setattr(service_app, "_answer_question", answer_question)
    monkeypatch.setattr(service_app, "compute_final_answer", lambda *args: type("Answer", (), {"text": "ok", "sources": [], "charts": [], "timing": {}, "trace": {}})())
    monkeypatch.setattr(service_app, "_sse_events_from_final_answer", lambda _answer: iter(("event: done\ndata: ok\n\n",)))
    events = list(_stream_resolving_session_events(SessionStore(), object(), object(), "q", "live", None, limiter=None))
    steps = _step_payloads(events)
    assert steps[0]["raw_name"] == "question_received"
    assert [step["status"] for step in steps[:2]] == ["started", "done"]
