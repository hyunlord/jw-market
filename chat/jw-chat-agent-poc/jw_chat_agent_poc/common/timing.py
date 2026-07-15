from __future__ import annotations

from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import os
import sys
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


class _StdoutHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(self.format(record), file=sys.stdout, flush=True)
        except Exception:
            self.handleError(record)


STAGE_TIMING_LOGGER = logging.getLogger("jw_chat_agent_poc.stage_timing")
STAGE_TIMING_LOGGER.setLevel(logging.INFO)
STAGE_TIMING_LOGGER.propagate = False
if not STAGE_TIMING_LOGGER.handlers:
    _stage_timing_handler = _StdoutHandler()
    _stage_timing_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    STAGE_TIMING_LOGGER.addHandler(_stage_timing_handler)

_PUBLIC_STAGE_NAMES = {
    "question_received": "질문 접수",
    "queue_wait": "대기 중",
    "question_classification": "질문 분류",
    "file_session_probe": "첨부 파일 확인",
    "file_schema_probe": "첨부 파일 구조 분석",
    "mixed_file_leg": "첨부 문서 조회",
    "mixed_market_leg": "시장 데이터 조회",
    "question_decomposition": "질문 분해",
    "market_snapshot": "시장 데이터 준비",
    "agent_pre_resolve": "질문 해석",
    "llm_plan": "분석 계획",
    "deterministic_plan": "조회 계획 확정",
    "strict_query_plan": "데이터 조회 설계",
    "completion_queries": "추가 지표 조회",
    "answer_contract_preflight": "필수 근거 확인",
    "bq_analysis": "시장 분석 정리",
    "tool_batch": "관련 데이터 조회",
    "compute": "지표 계산",
    "context_retrieval": "관련 이슈 수집",
    "fact_assembly": "근거 정리",
    "final_llm_expression": "답변 작성",
    "final_deterministic_fast_path": "답변 작성",
    "final_deterministic_single_period_sales_path": "답변 작성",
    "final_llm_retry": "답변 재작성",
    "answer_safety": "숫자 검증",
    "answer_generation_total": "답변 생성 전체",
    "answer_cleanup": "답변 정리",
    "chart_generation": "차트 준비",
}

_PUBLIC_TOOL_NAMES = {
    "get_brand_metric": "시장 데이터 집계",
    "get_brand_sales": "브랜드 매출 조회",
    "get_brand_share": "브랜드 점유율 확인",
    "get_brand_series": "브랜드 추이 확인",
    "get_top_brands": "상위 브랜드 확인",
    "get_market_landscape": "경쟁 구도 조회",
    "clinicaltrials_v2_search": "임상 데이터 조회",
    "clinical_scope_notice": "임상 조회 범위 확인",
    "competitor_molecule_candidates": "경쟁 성분 확인",
    "mfds_clinical_trial_kr": "국내 임상 정보 확인",
    "mfds_permission_search": "식약처 허가 정보 확인",
    "mfds_patent": "의약품 특허 정보 확인",
    "mfds_fda_orangebook": "FDA 특허 정보 확인",
    "openfda_label_search": "FDA 안전성 정보 확인",
    "openfda_combo_label_search": "FDA 복합제 안전성 정보 확인",
    "hira_disease": "건강보험 환자 정보 확인",
    "matching_policy_notice": "의약품 일치 기준 확인",
    "web_search": "최신 웹 자료 검색",
}


@dataclass(slots=True)
class StageProgress:
    summary: str | None = None

_PUBLIC_STAGE_DETAILS = {
    "request processing": "전체 처리 진행",
    "active uploaded file check": "현재 대화의 첨부 파일 확인",
    "active uploaded file schema check": "파일의 시트와 열 확인",
    "uploaded file retrieval": "첨부 문서 근거 조회",
    "market fact retrieval": "시장 데이터 근거 조회",
    "view selection": "시장 기준 판정",
    "agent setup": "분석 구성 준비",
    "BQ and tool routing": "질문 유형·도구 경로 판정",
    "tool catalog and market snapshot": "조회 도구·시장 데이터 준비",
    "brand and period grounding": "브랜드·기간 확인",
    "population-sensitive spec mapping": "질문 조건 반영",
    "deterministic metric backfill": "누락 지표 보강",
    "required fact backfill": "필수 근거 보강",
    "BQ analysis synthesis": "시장 분석 결과 정리",
    "parallel tool execution": "관련 자료 병렬 조회",
    "deterministic deltas and comparisons": "변화율·비교 계산",
    "background issue material": "뉴스·이슈 보조 근거",
    "markdown fact set build": "답변 근거 정리",
    "GenOS markdown generation": "최종 문장 생성",
    "verified top-N answer rendering": "검증된 상위 브랜드 표 조립",
    "verified single-period sales answer rendering": "검증된 단일기간 매출 답변 조립",
    "missing mandatory facts": "필수 근거 보강",
    "fact-number validation": "fact 숫자 대조",
    "GenOS expression plus safety": "표현 생성 및 검증",
    "markdown cleanup": "표기 정리",
    "fact-backed chart spec": "fact 기반 차트 준비",
    "molecule_trend": "성분 기준 임상시험 확인",
    "combo_and": "복합 성분 임상시험 확인",
    "metric=sales": "매출 데이터 확인",
}


def _public_stage_name(name: str) -> str:
    if name.startswith("tool:"):
        tool_name = name.removeprefix("tool:")
        return _PUBLIC_TOOL_NAMES.get(tool_name, "관련 데이터 조회")
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
        STAGE_TIMING_LOGGER.info(
            "stage_timing name=%s detail=%s elapsed_ms=%.3f",
            name,
            detail,
            elapsed_ms,
        )
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
