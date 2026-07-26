from __future__ import annotations

import json
import threading
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


def test_mixed_stage_names_are_user_facing() -> None:
    events: list[dict] = []
    with timing.stage(None, "file_session_probe", "active uploaded file check", sink=events.append):
        pass
    with timing.stage(None, "mixed_file_leg", "uploaded file retrieval", sink=events.append):
        pass
    with timing.stage(None, "mixed_market_leg", "market fact retrieval", sink=events.append):
        pass

    assert [event["name"] for event in events[::2]] == [
        "첨부 파일 확인",
        "첨부 문서 조회",
        "시장 데이터 조회",
    ]
    assert [event["detail"] for event in events[::2]] == [
        "현재 대화의 첨부 파일 확인",
        "첨부 문서 근거 조회",
        "시장 데이터 근거 조회",
    ]


def test_file_schema_and_tool_steps_hide_internal_names() -> None:
    events: list[dict] = []
    with timing.stage(None, "file_schema_probe", "active uploaded file schema check", sink=events.append):
        pass
    with timing.stage(None, "tool:clinicaltrials_v2_search", "molecule_trend", sink=events.append):
        pass
    with timing.stage(None, "tool:mfds_permission_search", "리바로", sink=events.append):
        pass
    with timing.stage(None, "tool:get_brand_metric", "metric=sales", sink=events.append):
        pass

    started = events[::2]
    assert [event["name"] for event in started] == [
        "첨부 파일 구조 분석",
        "ClinicalTrials.gov 조회 중",
        "NeDrug 허가정보 조회 중",
        "시장 데이터 집계",
    ]
    assert started[0]["detail"] == "파일의 시트와 열 확인"
    assert started[1]["detail"] == "성분 기준 임상시험 확인"
    public_text = str([{"name": event["name"], "detail": event["detail"]} for event in started])
    assert "file_schema_probe" not in public_text
    assert "clinicaltrials_v2_search" not in public_text
    assert "mfds_permission_search" not in public_text


def test_runtime_planning_steps_have_user_facing_labels() -> None:
    events: list[dict] = []
    stages = (
        ("deterministic_plan", "브랜드=리바로; 기간=2025-04"),
        ("answer_contract_preflight", "required fact backfill"),
        ("bq_analysis", "BQ analysis synthesis"),
        ("tool_batch", "parallel tool execution"),
    )

    for name, detail in stages:
        with timing.stage(None, name, detail, sink=events.append):
            pass

    started = events[::2]
    assert [event["name"] for event in started] == [
        "조회 계획 확정",
        "필수 근거 확인",
        "시장 분석 정리",
        "관련 데이터 조회",
    ]
    assert [event["detail"] for event in started] == [
        "브랜드=리바로; 기간=2025-04",
        "필수 근거 보강",
        "시장 분석 결과 정리",
        "관련 자료 병렬 조회",
    ]
    public_text = str([{"name": event["name"], "detail": event["detail"]} for event in started])
    assert not any(name in public_text for name, _detail in stages)


def test_parallel_step_metadata_is_projected_to_user_language() -> None:
    events: list[dict] = []
    with timing.stage(None, "deep_research_tool_batch", "step=1; mode=parallel", sink=events.append):
        pass

    started = events[0]
    assert started["detail"] == "1단계 · 관련 항목 동시 조회"
    assert "mode=" not in started["detail"]
    assert ";" not in started["detail"]


def test_runtime_market_tool_names_have_specific_public_labels() -> None:
    events: list[dict] = []
    expected = {
        "get_brand_sales": "브랜드 매출 조회",
        "get_brand_share": "브랜드 점유율 확인",
        "get_brand_series": "브랜드 추이 확인",
        "get_top_brands": "상위 브랜드 확인",
    }

    for raw_name in expected:
        with timing.stage(None, f"tool:{raw_name}", "리바로", sink=events.append):
            pass

    started = events[::2]
    assert [event["name"] for event in started] == list(expected.values())
    public_text = str([event["name"] for event in started])
    assert not any(raw_name in public_text for raw_name in expected)


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
    assert steps[0]["name"] == "대기 중"
    assert "raw_name" not in steps[0]
    assert "raw_detail" not in steps[0]
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
    assert steps[0]["name"] == "질문 접수"
    assert "raw_name" not in steps[0]
    assert "raw_detail" not in steps[0]
    assert [step["status"] for step in steps[:2]] == ["started", "done"]


def test_stream_step_boundary_never_exposes_internal_stage_labels(monkeypatch) -> None:
    def answer_question(*args, **kwargs):
        with timing.stage(None, "file_schema_probe", "active uploaded file schema check"):
            pass
        return {"question": "q", "result": {"answer": "ok", "sources": [], "tool_calls": []}, "conversation_id": "c"}

    monkeypatch.setattr(service_app, "_answer_question", answer_question)
    monkeypatch.setattr(service_app, "compute_final_answer", lambda *args: type("Answer", (), {"text": "ok", "sources": [], "charts": [], "timing": {}, "trace": {}})())
    monkeypatch.setattr(service_app, "_sse_events_from_final_answer", lambda _answer: iter(("event: done\ndata: ok\n\n",)))

    events = list(_stream_resolving_session_events(SessionStore(), object(), object(), "q", "live", None, limiter=None))
    schema_steps = [step for step in _step_payloads(events) if step["name"] == "첨부 파일 구조 분석"]

    assert schema_steps
    assert all("raw_name" not in step for step in schema_steps)
    assert all("raw_detail" not in step for step in schema_steps)


def test_deep_stream_has_distinct_public_progress_without_internal_names(monkeypatch) -> None:
    def answer_question(*args, **kwargs):
        with timing.stage(
            None,
            "deep_research_plan",
            "tool catalog and market snapshot",
            sink=kwargs["timing_sink"],
        ) as progress:
            progress.summary = "get_metric -> search_clinical -> web_search"
        return {
            "question": "/deep 리바로 경쟁구도",
            "result": {"answer": "ok", "sources": [], "tool_calls": []},
            "conversation_id": "c",
        }

    monkeypatch.setattr(service_app, "_answer_question", answer_question)
    monkeypatch.setattr(
        service_app,
        "compute_final_answer",
        lambda *args: type(
            "Answer",
            (),
            {"text": "ok", "sources": [], "charts": [], "timing": {}, "trace": {}},
        )(),
    )
    monkeypatch.setattr(
        service_app,
        "_sse_events_from_final_answer",
        lambda _answer: iter(("event: done\ndata: ok\n\n",)),
    )

    events = list(
        _stream_resolving_session_events(
            SessionStore(),
            object(),
            object(),
            "/deep 리바로 경쟁구도",
            "live",
            None,
            limiter=None,
        )
    )
    steps = _step_payloads(events)
    public_text = str(steps)

    assert "딥리서치 전체" in {step["name"] for step in steps}
    assert "답변 생성 전체" not in {step["name"] for step in steps}
    assert "시장 지표 조회 → 임상시험 통합 조회 → 최신 웹 자료 검색" in {
        step.get("summary") for step in steps
    }
    assert all("raw_name" not in step and "raw_detail" not in step for step in steps)
    assert "get_metric" not in public_text
    assert "search_clinical" not in public_text
    assert "web_search" not in public_text


def _verified_external_result() -> dict:
    return {
        "answer": "최종 분석입니다.",
        "sources": ["ClinicalTrials.gov", "식약처"],
        "tool_calls": [
            {
                "tool": "clinicaltrials_v2_search",
                "status": "live",
                "render_data": {"ok": True, "evidence": [{"metric": "글로벌 임상시험"}]},
            },
            {
                "tool": "mfds_permission_search",
                "status": "live",
                "render_data": {"ok": True, "items": [{"item_name": "리바로정"}]},
            },
        ],
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
    }


def test_verified_evidence_progress_stripper_accepts_spacing_variants() -> None:
    progress = (
        "임상시험 1건 · 허가 1건의 근거를 확인했습니다.\n"
        "확인된 자료를 종합해 답변을 정리하고 있어요.  "
    )

    assert service_app._strip_verified_evidence_progress(f"{progress}최종 분석입니다.") == "최종 분석입니다."
    assert service_app._strip_verified_evidence_progress("최종 분석입니다.") == "최종 분석입니다."


def test_verified_evidence_progress_is_not_part_of_final_answer() -> None:
    result = _verified_external_result()

    final = service_app.compute_final_answer("리바로 임상과 허가", result, "c")

    assert "근거를 확인했습니다" not in final.text
    assert "답변을 정리하고 있어요" not in final.text


def test_verified_evidence_progress_is_removed_after_whitespace_cleanup() -> None:
    result = _verified_external_result()
    result["answer"] = (
        "임상시험 1건·허가 1건의 근거를 확인했습니다.\n"
        "확인된 자료를 종합해 답변을 정리하고 있어요.  최종 분석입니다."
    )

    final = service_app.compute_final_answer("리바로 임상과 허가", result, "c")

    assert "근거를 확인했습니다" not in final.text
    assert "답변을 정리하고 있어요" not in final.text


def test_verified_evidence_progress_is_not_streamed_as_answer_delta(monkeypatch) -> None:
    compute_started = threading.Event()
    compute_finished = threading.Event()

    def answer_question(*args, **kwargs):
        return {
            "question": "리바로 임상과 허가",
            "result": _verified_external_result(),
            "conversation_id": "c",
        }

    def compute_final_answer(*args):
        compute_started.set()
        time.sleep(0.15)
        compute_finished.set()
        return service_app.FinalAnswer(
            text="최종 분석입니다.",
            charts=[],
            timing={},
            trace={},
            sources=("ClinicalTrials.gov", "식약처"),
            conversation_id="c",
        )

    monkeypatch.setattr(service_app, "_answer_question", answer_question)
    monkeypatch.setattr(service_app, "compute_final_answer", compute_final_answer)

    stream = _stream_resolving_session_events(
        SessionStore(),
        object(),
        object(),
        "리바로 임상과 허가",
        "live",
        None,
        limiter=None,
    )
    events = list(stream)
    rendered = "".join(events)
    assert compute_started.is_set() is True
    assert compute_finished.is_set() is True
    assert "임상시험 1건·허가 1건의 근거를 확인했습니다" not in rendered
    assert "최종 분석입니다." in rendered
    assert "event: step\n" in rendered
