from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import threading
import time
from typing import Any, Callable, Iterator

from jw_chat_agent_poc.common.token_usage import public_token_usage


Timing = MutableMapping[str, Any]
StageEventSink = Callable[[dict[str, Any]], None]
_ACTIVE_STAGE_SINK: ContextVar[StageEventSink | None] = ContextVar("active_stage_sink", default=None)
STEP_HEARTBEAT_THRESHOLD_S_ENV = "STEP_HEARTBEAT_THRESHOLD_S"
DEFAULT_STEP_HEARTBEAT_THRESHOLD_S = 3.0
STEP_HEARTBEAT_INTERVAL_S = 2.5

_PUBLIC_STAGE_NAMES = {
    "question_received": "질문 접수",
    "queue_wait": "대기 중",
    "question_classification": "질문 분류",
    "question_decomposition": "질문 분해",
    "market_snapshot": "시장 데이터 준비",
    "agent_pre_resolve": "질문 해석",
    "llm_plan": "분석 계획",
    "strict_query_plan": "데이터 조회 설계",
    "completion_queries": "추가 지표 조회",
    "compute": "지표 계산",
    "context_retrieval": "관련 이슈 수집",
    "fact_assembly": "근거 정리",
    "final_llm_expression": "답변 작성",
    "final_llm_retry": "답변 재작성",
    "answer_safety": "숫자 검증",
    "answer_generation_total": "답변 생성 전체",
    "answer_cleanup": "답변 정리",
    "chart_generation": "차트 준비",
}


@dataclass(slots=True)
class StageProgress:
    summary: str | None = None

_PUBLIC_STAGE_DETAILS = {
    "request processing": "전체 처리 진행",
    "view selection": "시장 기준 판정",
    "agent setup": "분석 구성 준비",
    "BQ and tool routing": "질문 유형·도구 경로 판정",
    "tool catalog and market snapshot": "조회 도구·시장 데이터 준비",
    "brand and period grounding": "브랜드·기간 확인",
    "population-sensitive spec mapping": "질문 조건 반영",
    "deterministic metric backfill": "누락 지표 보강",
    "deterministic deltas and comparisons": "변화율·비교 계산",
    "background issue material": "뉴스·이슈 보조 근거",
    "markdown fact set build": "답변 근거 정리",
    "GenOS markdown generation": "최종 문장 생성",
    "missing mandatory facts": "필수 근거 보강",
    "fact-number validation": "fact 숫자 대조",
    "GenOS expression plus safety": "표현 생성 및 검증",
    "markdown cleanup": "표기 정리",
    "fact-backed chart spec": "fact 기반 차트 준비",
}


def _public_stage_name(name: str) -> str:
    if name.startswith("tool:"):
        return f"도구 실행({name.removeprefix('tool:')})"
    return _PUBLIC_STAGE_NAMES.get(name, name)


def _public_stage_detail(detail: str) -> str:
    if detail.startswith("step="):
        return f"{detail.removeprefix('step=')}단계"
    return _PUBLIC_STAGE_DETAILS.get(detail, detail)


def new_timing() -> dict[str, Any]:
    """Create a request-local timing payload for user-visible latency reporting."""

    return {"started_at_monotonic": time.perf_counter(), "stages": []}


def ensure_timing(result: MutableMapping[str, Any]) -> Timing:
    """Return a mutable timing payload on an agent result."""

    timing = result.get("timing")
    if not isinstance(timing, dict):
        timing = new_timing()
        result["timing"] = timing
    timing.setdefault("stages", [])
    timing.setdefault("started_at_monotonic", time.perf_counter())
    return timing


@contextmanager
def stage(
    timing: Timing | None,
    name: str,
    detail: str = "",
    sink: StageEventSink | None = None,
) -> Iterator[StageProgress]:
    """Record elapsed milliseconds for one named processing stage."""

    effective_sink = sink or _ACTIVE_STAGE_SINK.get()
    started = time.perf_counter()
    progress = StageProgress()
    heartbeat_stop = threading.Event()
    _emit_stage_event(effective_sink, name, detail, "started")
    _start_heartbeat(effective_sink, name, detail, started, heartbeat_stop)
    try:
        yield progress
    finally:
        heartbeat_stop.set()
        elapsed_ms = (time.perf_counter() - started) * 1000
        add_stage(timing, name, elapsed_ms, detail)
        _emit_stage_event(effective_sink, name, detail, "done", elapsed_ms, summary=progress.summary)


def _start_heartbeat(
    sink: StageEventSink | None,
    name: str,
    detail: str,
    started: float,
    stop: threading.Event,
) -> threading.Thread | None:
    if sink is None:
        return None
    try:
        threshold = float(os.environ.get(STEP_HEARTBEAT_THRESHOLD_S_ENV, DEFAULT_STEP_HEARTBEAT_THRESHOLD_S))
    except ValueError:
        threshold = DEFAULT_STEP_HEARTBEAT_THRESHOLD_S
    threshold = max(0.0, threshold)

    def emit_until_done() -> None:
        if stop.wait(threshold):
            return
        while not stop.is_set():
            elapsed_ms = (time.perf_counter() - started) * 1000
            try:
                _emit_stage_event(sink, name, detail, "in_progress", elapsed_ms)
            except Exception:
                pass
            if stop.wait(STEP_HEARTBEAT_INTERVAL_S):
                return

    heartbeat = threading.Thread(target=emit_until_done, name=f"step-heartbeat-{name}", daemon=True)
    heartbeat.start()
    return heartbeat


@contextmanager
def stage_event_sink(sink: StageEventSink | None) -> Iterator[None]:
    """Temporarily attach a request-local progress sink for nested stage calls."""

    token = _ACTIVE_STAGE_SINK.set(sink)
    try:
        yield
    finally:
        _ACTIVE_STAGE_SINK.reset(token)


def _emit_stage_event(
    sink: StageEventSink | None,
    name: str,
    detail: str,
    status: str,
    elapsed_ms: float | None = None,
    *,
    summary: str | None = None,
) -> None:
    if sink is None:
        return
    event: dict[str, Any] = {
        "name": _public_stage_name(name),
        "detail": _public_stage_detail(detail),
        "status": status,
        "raw_name": name,
        "raw_detail": detail,
    }
    if elapsed_ms is not None:
        event["elapsed_ms"] = round(float(elapsed_ms), 2)
    if summary:
        event["summary"] = summary
    sink(event)


def add_stage(timing: Timing | None, name: str, elapsed_ms: float, detail: str = "") -> None:
    if timing is None:
        return
    stages = timing.setdefault("stages", [])
    if not isinstance(stages, list):
        stages = []
        timing["stages"] = stages
    stages.append({"name": name, "elapsed_ms": round(float(elapsed_ms), 2), "detail": detail})


def finish(timing: Timing | None) -> dict[str, Any]:
    """Finalize total elapsed time without losing per-stage entries."""

    if timing is None:
        return {}
    started = timing.get("started_at_monotonic")
    if isinstance(started, int | float):
        timing["total_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    else:
        timing.setdefault("total_elapsed_ms", 0.0)
    return public_payload(timing)


def public_payload(timing: Timing | None) -> dict[str, Any]:
    if not isinstance(timing, MutableMapping):
        return {"total_elapsed_ms": 0.0, "stages": []}
    stages = timing.get("stages") if isinstance(timing.get("stages"), list) else []
    return {
        "total_elapsed_ms": round(float(timing.get("total_elapsed_ms") or 0.0), 2),
        "stages": [
            {
                "name": _public_stage_name(str(item.get("name") or "")),
                "elapsed_ms": round(float(item.get("elapsed_ms") or 0.0), 2),
                "detail": _public_stage_detail(str(item.get("detail") or "")),
            }
            for item in stages
            if isinstance(item, dict)
        ],
        "token_usage": public_token_usage(timing),
    }


def markdown_block(timing: Timing | None) -> str:
    payload = public_payload(timing)
    total_ms = float(payload.get("total_elapsed_ms") or 0.0)
    rows = [
        "## 처리 시간",
        "",
        f"- 총 소요: {total_ms / 1000:.2f}초",
        "",
        "| 단계 | 소요 | 비고 |",
        "| --- | ---: | --- |",
    ]
    for item in payload.get("stages", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        elapsed_ms = float(item.get("elapsed_ms") or 0.0)
        detail = str(item.get("detail") or "")
        rows.append(f"| {name} | {elapsed_ms / 1000:.2f}초 | {detail} |")
    return "\n".join(rows)
