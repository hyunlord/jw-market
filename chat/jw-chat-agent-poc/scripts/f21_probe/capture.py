from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import time
from urllib.parse import urljoin

import requests

from scripts.f21_probe.artifacts import atomic_json, atomic_text, utc_now
from scripts.f21_probe.schema import QUESTION_ANSWER_SCHEMA
from scripts.f21_probe.sse import (
    JsonObject,
    event_names,
    extract_tools,
    latest_object,
    object_value,
    parse_sse,
    render_answer,
)
from scripts.f21_probe.types import OutputRow, RunOptions


def capture_turn(
    *,
    root: Path,
    relative: Path,
    endpoint: str,
    headers: dict[str, str],
    timeout_seconds: float,
    stage: str,
    case_id: str,
    question: str,
    session_id: str,
    repetition: int | None,
    turn: int,
) -> OutputRow:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    sse_path = destination.with_suffix(".sse")
    json_path = destination.with_suffix(".json")
    started_utc = utc_now()
    started = time.perf_counter()
    raw = ""
    http_status: int | None = None
    error: JsonObject | None = None
    try:
        response = requests.post(
            endpoint,
            json={"question": question, "conversationId": session_id},
            headers={"Accept": "text/event-stream", **headers},
            timeout=timeout_seconds,
        )
        http_status = response.status_code
        raw = response.content.decode("utf-8", errors="replace")
        response.raise_for_status()
    except requests.RequestException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        response_value = getattr(exc, "response", None)
        if response_value is not None and response_value.content:
            raw = response_value.content.decode("utf-8", errors="replace")
            http_status = response_value.status_code
    elapsed_seconds = round(time.perf_counter() - started, 3)
    atomic_text(sse_path, raw)

    events = parse_sse(raw)
    answer = render_answer(events)
    trace = latest_object(events, "trace")
    timing = latest_object(events, "timing")
    qa_trace = object_value(trace, "qa_trace")
    request = object_value(qa_trace, "request")
    final = object_value(qa_trace, "final")
    row: OutputRow = {
        "schema": QUESTION_ANSWER_SCHEMA,
        "stage": stage,
        "case_id": case_id,
        "repetition": repetition,
        "turn": turn,
        "question": question,
        "conversation_id": session_id,
        "conversation_id_sha256": sha256(session_id.encode()).hexdigest(),
        "pod": request.get("pod"),
        "trace_id": request.get("request_id") or trace.get("trace_id"),
        "disposition": final.get("disposition"),
        "tools_called": extract_tools(qa_trace, trace),
        "answer_full": answer,
        "answer_sha256": sha256(answer.encode()).hexdigest(),
        "timing": timing,
        "total_elapsed_ms": timing.get("total_elapsed_ms"),
        "client_elapsed_s": elapsed_seconds,
        "http_status": http_status,
        "error": error,
        "qa_trace": qa_trace,
        "trace": trace,
        "router_diagnostics": latest_object(events, "router_diagnostics"),
        "conversation_event": latest_object(events, "conversation"),
        "event_names": event_names(events),
        "sse_file": sse_path.relative_to(root).as_posix(),
        "sse_raw": raw,
        "started_utc": started_utc,
        "finished_utc": utc_now(),
    }
    atomic_json(json_path, row)
    return row


def cleanup_sessions(options: RunOptions, sessions: list[str]) -> JsonObject:
    if not options.cleanup_url:
        return {
            "enabled": False,
            "attempted": 0,
            "statuses": [],
            "http_failures": 0,
            "request_errors": [],
        }
    statuses: list[int] = []
    errors: list[JsonObject] = []
    for session_id in sessions:
        try:
            response = requests.put(
                options.cleanup_url,
                headers={"Content-Type": "application/json", **options.headers},
                json={"uid": session_id},
                timeout=min(options.request_timeout_seconds, 60.0),
            )
            statuses.append(response.status_code)
        except requests.RequestException as exc:
            errors.append({"type": type(exc).__name__, "message": str(exc)})
    return {
        "enabled": True,
        "attempted": len(sessions),
        "statuses": statuses,
        "http_failures": sum(code != 200 for code in statuses),
        "request_errors": errors,
    }


def stream_endpoint(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
