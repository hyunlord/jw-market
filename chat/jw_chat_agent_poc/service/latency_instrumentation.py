from __future__ import annotations

import gc
import json
import logging
import os
import resource
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Final


class _StdoutHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(self.format(record), file=sys.stdout, flush=True)
        except (OSError, UnicodeError):
            self.handleError(record)


LOGGER = logging.getLogger("jw_chat_agent_poc.r73b_latency")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
if not any(isinstance(handler, _StdoutHandler) for handler in LOGGER.handlers):
    _latency_handler = _StdoutHandler()
    _latency_handler.setLevel(logging.INFO)
    _latency_handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(_latency_handler)

LATENCY_INSTRUMENTATION_ENV: Final = "CHAT_R73B_LATENCY_INSTRUMENTATION"
_TRUE_VALUES: Final = frozenset({"1", "true", "on", "enabled", "yes"})
_MAX_ACTIVE_PROBES: Final = 128
_MAX_PRE_T0_MARKS: Final = 256


def latency_instrumentation_enabled() -> bool:
    return os.getenv(LATENCY_INSTRUMENTATION_ENV, "false").strip().casefold() in _TRUE_VALUES


def record_first_answer_delta(
    *,
    conversation_id: str,
    question: str,
    elapsed_ms: float,
    output_bytes: int,
) -> None:
    """Record the first user-readable answer frame at the actual SSE boundary."""

    if not latency_instrumentation_enabled():
        return
    payload = {
        "schema": "r73g_ttfa_v1",
        "conversation_hash": sha256(conversation_id.encode("utf-8")).hexdigest()[:16],
        "question_hash": sha256(question.encode("utf-8")).hexdigest()[:16],
        "event": "checkpoint",
        "stage": "transport.first_answer_delta",
        "status": "ok",
        "elapsed_ms": round(elapsed_ms, 3),
        "output_bytes": output_bytes,
        "unit": "ms",
    }
    LOGGER.info(
        "r73b_latency %s",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )


def _rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _gc_collections() -> tuple[int, ...]:
    return tuple(int(row.get("collections", 0)) for row in gc.get_stats())


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


class LatencyProbe:
    def __init__(
        self,
        conversation_id: str,
        *,
        input_bytes: int | None,
        question: str | None,
        record_count: int | None,
    ) -> None:
        self._conversation_hash = sha256(conversation_id.encode("utf-8")).hexdigest()[:16]
        self._question_hash = (
            sha256(question.encode("utf-8")).hexdigest()[:16] if question else None
        )
        self._last_wall = time.perf_counter()
        self._last_cpu = time.process_time()
        self._last_gc = _gc_collections()
        self._last_output_bytes = input_bytes
        self._record_count = record_count
        self._probe_overhead_ms = 0.0
        self._started_wall = self._last_wall
        self._started_cpu = self._last_cpu
        self._emit(
            {
                "event": "begin",
                "stage": "runtime.t0",
                "status": "ok",
                "input_bytes": input_bytes,
                "output_bytes": input_bytes,
                "record_count": record_count,
                "rss_bytes": _rss_bytes(),
            },
            logging.INFO,
        )
        self._reset_boundary()

    def checkpoint(
        self,
        stage: str,
        *,
        input_bytes: int | None = None,
        output_bytes: int | None = None,
        output_value: Any | None = None,
        object_count: int | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> int | None:
        boundary_wall = time.perf_counter()
        boundary_cpu = time.process_time()
        boundary_gc = _gc_collections()
        event: dict[str, Any] = {
            "event": "checkpoint",
            "stage": stage,
            "status": "ok",
            "wall_ms": round((boundary_wall - self._last_wall) * 1000, 3),
            "cpu_ms": round((boundary_cpu - self._last_cpu) * 1000, 3),
            "input_bytes": self._last_output_bytes if input_bytes is None else input_bytes,
            "output_bytes": output_bytes,
            "object_count": object_count,
            "record_count": self._record_count,
            "gc_collections": [
                current - previous
                for current, previous in zip(boundary_gc, self._last_gc, strict=True)
            ],
            "rss_bytes": _rss_bytes(),
            "allocation_bytes": None,
            "allocation_status": "not_measured_without_tracemalloc",
        }
        if fields:
            event["fields"] = dict(fields)

        measurement_started = time.perf_counter()
        level = logging.INFO
        if output_value is not None:
            try:
                event["output_bytes"] = _json_bytes(output_value)
            except Exception as exc:  # noqa: BLE001 - a probe must not fail the answer
                event["status"] = "unknown"
                event["error_type"] = type(exc).__name__
                level = logging.WARNING
        self._emit(event, level)
        self._probe_overhead_ms += (time.perf_counter() - measurement_started) * 1000
        if isinstance(event.get("output_bytes"), int):
            self._last_output_bytes = int(event["output_bytes"])
        self._last_wall = time.perf_counter()
        self._last_cpu = time.process_time()
        self._last_gc = _gc_collections()
        measured_output = event.get("output_bytes")
        return int(measured_output) if isinstance(measured_output, int) else None

    def finish(self) -> None:
        now_wall = time.perf_counter()
        now_cpu = time.process_time()
        self._emit(
            {
                "event": "finish",
                "stage": "request.complete",
                "status": "ok",
                "measured_wall_ms": round((now_wall - self._started_wall) * 1000, 3),
                "measured_cpu_ms": round((now_cpu - self._started_cpu) * 1000, 3),
                "probe_overhead_ms": round(self._probe_overhead_ms, 3),
                "rss_bytes": _rss_bytes(),
            },
            logging.INFO,
        )

    def _reset_boundary(self) -> None:
        self._last_wall = time.perf_counter()
        self._last_cpu = time.process_time()
        self._last_gc = _gc_collections()

    def _emit(self, event: Mapping[str, Any], level: int) -> None:
        payload = {
            "schema": "r73b_latency_v1",
            "conversation_hash": self._conversation_hash,
            "question_hash": self._question_hash,
            **event,
        }
        LOGGER.log(
            level,
            "r73b_latency %s",
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )


class PreAnswerTimeline:
    """Buffer the stage boundaries that run *before* the T0 probe exists.

    ``LatencyProbe`` starts at T0 (the end of ``V4Runtime.answer``), so every
    stage that produces the answer -- planner, fan-out, synthesis -- has never
    been measured.  This class closes that gap without distorting it: ``mark``
    pays two clock reads and one append, and every serialization is deferred to
    ``flush`` at T0.  A probe that costs what it measures reports a fiction.

    ``flush`` also reconciles: the sum of the spans is compared against the wall
    clock from entry to T0 and any residue is emitted as ``unattributed_ms``
    rather than silently absorbed into a neighbouring stage.
    """

    __slots__ = (
        "_conversation_hash",
        "_flush_overhead_ms",
        "_flushed",
        "_marks",
        "_question_hash",
        "_started_cpu",
        "_started_wall",
    )

    def __init__(self, conversation_id: str, question: str | None) -> None:
        self._conversation_hash = sha256(conversation_id.encode("utf-8")).hexdigest()[:16]
        self._question_hash = (
            sha256(question.encode("utf-8")).hexdigest()[:16] if question else None
        )
        self._started_wall = time.perf_counter()
        self._started_cpu = time.process_time()
        self._flush_overhead_ms = 0.0
        self._flushed = False
        # (stage, wall, process_cpu, fields) -- fields stay by reference; they
        # are only walked at flush time.
        self._marks: list[tuple[str, float, float, Mapping[str, Any] | None]] = [
            ("pre_t0.entry", self._started_wall, self._started_cpu, None)
        ]

    def mark(self, stage: str, fields: Mapping[str, Any] | None = None) -> None:
        if len(self._marks) >= _MAX_PRE_T0_MARKS:
            return
        self._marks.append((stage, time.perf_counter(), time.process_time(), fields))

    def flush(self, *, extra: Mapping[str, Any] | None = None) -> None:
        if self._flushed:
            return
        self._flushed = True
        flush_started = time.perf_counter()
        total_wall_ms = (flush_started - self._started_wall) * 1000
        total_cpu_ms = (time.process_time() - self._started_cpu) * 1000
        spans_sum_ms = 0.0
        emitted = 0
        try:
            previous = self._marks[0]
            for stage, wall, cpu, fields in self._marks[1:]:
                wall_ms = (wall - previous[1]) * 1000
                spans_sum_ms += wall_ms
                event: dict[str, Any] = {
                    "event": "checkpoint",
                    "stage": stage,
                    "status": "ok",
                    "wall_ms": round(wall_ms, 3),
                    # process_time() is process-wide, so a fan-out span that
                    # runs on a thread pool can report more CPU than wall.
                    # Named so it cannot be mistaken for request-scoped CPU.
                    "process_cpu_ms": round((cpu - previous[2]) * 1000, 3),
                    "offset_ms": round((wall - self._started_wall) * 1000, 3),
                }
                if fields:
                    event["fields"] = _safe_fields(fields)
                self._emit(event, logging.INFO)
                emitted += 1
                previous = (stage, wall, cpu, fields)
        except Exception as exc:  # noqa: BLE001 - a probe must not fail the answer
            self._emit(
                {
                    "event": "checkpoint",
                    "stage": "pre_t0.spans",
                    "status": "unknown",
                    "error_type": type(exc).__name__,
                    "emitted_spans": emitted,
                },
                logging.WARNING,
            )
        rounded_wall_ms = round(total_wall_ms, 3)
        rounded_spans_sum_ms = round(spans_sum_ms, 3)
        summary: dict[str, Any] = {
            "event": "checkpoint",
            "stage": "pre_t0.total",
            "status": "ok",
            "wall_ms": rounded_wall_ms,
            "process_cpu_ms": round(total_cpu_ms, 3),
            "spans_sum_ms": rounded_spans_sum_ms,
            # Reconciliation residue. Non-zero means a region of the pre-T0 path
            # carries no boundary yet; it is reported, never absorbed.
            "unattributed_ms": round(rounded_wall_ms - rounded_spans_sum_ms, 3),
            "span_count": emitted,
            "marks_truncated": len(self._marks) >= _MAX_PRE_T0_MARKS,
            "rss_bytes": _rss_bytes(),
        }
        if extra:
            summary["fields"] = _safe_fields(extra)
        self._emit(summary, logging.INFO)
        self._flush_overhead_ms = (time.perf_counter() - flush_started) * 1000
        self._emit(
            {
                "event": "checkpoint",
                "stage": "pre_t0.probe_overhead",
                "status": "ok",
                # Milliseconds. The gate for this round is 2000 ms.
                "probe_overhead_ms": round(self._flush_overhead_ms, 3),
            },
            logging.INFO,
        )

    def _emit(self, event: Mapping[str, Any], level: int) -> None:
        payload = {
            "schema": "r73d_pre_t0_v1",
            "conversation_hash": self._conversation_hash,
            "question_hash": self._question_hash,
            **event,
        }
        LOGGER.log(
            level,
            "r73b_latency %s",
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )


class _DisabledPreAnswerTimeline:
    def mark(self, _stage: str, _fields: Mapping[str, Any] | None = None) -> None:
        return None

    def flush(self, *, extra: Mapping[str, Any] | None = None) -> None:
        return None


_DISABLED_TIMELINE = _DisabledPreAnswerTimeline()


def begin_pre_answer_timeline(
    conversation_id: str,
    question: str | None = None,
) -> PreAnswerTimeline | _DisabledPreAnswerTimeline:
    if not latency_instrumentation_enabled():
        return _DISABLED_TIMELINE
    return PreAnswerTimeline(conversation_id, question)


def _safe_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Never let a field value abort the flush; degrade the field, not the run."""
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        try:
            json.dumps(value, ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001 - a probe must not fail the answer
            safe[str(key)] = f"<unserializable:{type(exc).__name__}>"
        else:
            safe[str(key)] = value
    return safe


def lane_spans_from_execution_trace(trace: Any) -> dict[str, Any]:
    """Summarize an executor trace into per-lane wall time and concurrency.

    The executor already records ``started_ms``/``ended_ms`` per tool call; it
    just never reached the latency log.  Overlap of those intervals is what
    answers "is fan-out parallel or serial", so it is computed here rather than
    assumed.
    """
    if not isinstance(trace, Mapping):
        return {"status": "no_trace"}
    tools = trace.get("tools")
    if not isinstance(tools, list):
        return {"status": "no_tools", "elapsed_ms": trace.get("elapsed_ms")}
    lanes: dict[str, dict[str, Any]] = {}
    intervals: list[tuple[float, float]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        source = str(tool.get("source", "unknown"))
        lane = lanes.setdefault(
            source,
            {"calls": 0, "cache_hits": 0, "busy_ms": 0.0, "max_call_ms": 0.0, "statuses": {}},
        )
        lane["calls"] += 1
        if tool.get("cache_hit"):
            lane["cache_hits"] += 1
        status = str(tool.get("status", "unknown"))
        lane["statuses"][status] = lane["statuses"].get(status, 0) + 1
        start = tool.get("started_ms")
        end = tool.get("ended_ms")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
            duration = float(end) - float(start)
            lane["busy_ms"] += duration
            lane["max_call_ms"] = max(float(lane["max_call_ms"]), duration)
            if duration > 0:
                intervals.append((float(start), float(end)))
    for lane in lanes.values():
        lane["busy_ms"] = round(float(lane["busy_ms"]), 3)
        lane["max_call_ms"] = round(float(lane["max_call_ms"]), 3)
    busy_sum_ms = round(sum(float(lane["busy_ms"]) for lane in lanes.values()), 3)
    wall_ms = trace.get("elapsed_ms")
    span_ms = 0.0
    if intervals:
        # Seed from the *sorted* head. Seeding from intervals[0] in list order
        # silently under-reports whenever the first tool submitted is not the
        # first to start, which is the normal case for a tiered fan-out.
        ordered = sorted(intervals)
        merged_start, merged_end = ordered[0]
        span_ms = 0.0
        for start, end in ordered[1:]:
            if start > merged_end:
                span_ms += merged_end - merged_start
                merged_start, merged_end = start, end
            else:
                merged_end = max(merged_end, end)
        span_ms += merged_end - merged_start
    return {
        "status": "ok",
        "elapsed_ms": wall_ms,
        "tool_calls": len(tools),
        "lanes": lanes,
        "busy_sum_ms": busy_sum_ms,
        "union_busy_ms": round(span_ms, 3),
        # >1 means calls overlapped in time, i.e. the fan-out really is parallel.
        "parallelism_ratio": (
            round(busy_sum_ms / span_ms, 3) if span_ms > 0 else None
        ),
        "quorum_fired": trace.get("quorum_fired"),
        "quorum_fire_ms": trace.get("quorum_fire_ms"),
        "session_result_reused": trace.get("session_result_reused"),
    }


_ACTIVE_PROBES: OrderedDict[str, LatencyProbe] = OrderedDict()
_ACTIVE_PROBES_LOCK = threading.Lock()


def begin_latency_probe(
    conversation_id: str,
    *,
    input_bytes: int | None = None,
    question: str | None = None,
    record_count: int | None = None,
) -> LatencyProbe | _DisabledLatencyProbe:
    if not latency_instrumentation_enabled():
        return _DISABLED_PROBE
    probe = LatencyProbe(
        conversation_id,
        input_bytes=input_bytes,
        question=question,
        record_count=record_count,
    )
    with _ACTIVE_PROBES_LOCK:
        _ACTIVE_PROBES[conversation_id] = probe
        _ACTIVE_PROBES.move_to_end(conversation_id)
        while len(_ACTIVE_PROBES) > _MAX_ACTIVE_PROBES:
            _ACTIVE_PROBES.popitem(last=False)
    return probe


def get_latency_probe(conversation_id: str | None) -> LatencyProbe | None:
    if not conversation_id:
        return None
    with _ACTIVE_PROBES_LOCK:
        return _ACTIVE_PROBES.get(conversation_id)


def finish_latency_probe(conversation_id: str | None) -> None:
    if not conversation_id:
        return
    with _ACTIVE_PROBES_LOCK:
        probe = _ACTIVE_PROBES.pop(conversation_id, None)
    if probe is not None:
        probe.finish()


class _DisabledLatencyProbe:
    def checkpoint(self, _stage: str, **_kwargs: Any) -> int | None:
        return None


_DISABLED_PROBE = _DisabledLatencyProbe()


__all__ = [
    "LATENCY_INSTRUMENTATION_ENV",
    "LatencyProbe",
    "PreAnswerTimeline",
    "begin_latency_probe",
    "begin_pre_answer_timeline",
    "finish_latency_probe",
    "get_latency_probe",
    "lane_spans_from_execution_trace",
    "latency_instrumentation_enabled",
    "record_first_answer_delta",
]
