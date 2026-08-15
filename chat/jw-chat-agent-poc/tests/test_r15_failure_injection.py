"""R15 failure injection — F3 and F5, both arms."""

from __future__ import annotations

import time

from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.runtime import _retrieval_shortfall_notice

QUESTION = "리바로 매출 알려줘"


def _plan(answer_sources=("mart",)) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=QUESTION,
        expanded_intents=(QUESTION,),
        answer_sources=tuple(answer_sources),
        tool_queries=ToolQueries(
            mart=(QUESTION,),
            nedrug=(QUESTION,),
            hira=(QUESTION,),
            openfda=(QUESTION,),
            clinicaltrials=(QUESTION,),
            web=(QUESTION,),
            patent=(QUESTION,),
        ),
        linking_plan="deterministic",
        requested_answer_shape=RequestedAnswerShape(
            entities=("리바로",),
            measure_or_attribute=("매출액",),
        ),
    )


def _ok(source):
    def adapter(query, **_kwargs):
        return SourceResult(
            source=source, query=query, status="ok", payload={"calls": [{"x": 1}]}
        )

    return adapter


def _build(mart_adapter):
    from jw_chat_agent_poc.service.v4.contracts import SOURCE_NAMES

    adapters = {name: _ok(name) for name in SOURCE_NAMES}
    adapters["mart"] = mart_adapter
    return ParallelSourceExecutor(
        adapters=adapters, per_tool_timeout_s=2.0, total_timeout_s=4.0
    )


def test_f5_a_raising_mart_lane_does_not_take_the_other_lanes_down() -> None:
    """F5 (injected arm): invariant 3 — one lane's failure is not the answer's."""

    def exploding_mart(query, **_kwargs):
        raise RuntimeError("injected mart failure")

    results = _build(exploding_mart).execute(_plan(), session_id="r15-f5")

    by_source = {result.source: result for result in results}
    assert by_source["mart"].status != "ok"
    survivors = [
        source
        for source, result in by_source.items()
        if source != "mart" and result.status == "ok"
    ]
    assert survivors, "every other lane was lost with mart"
    # The failure is reported, not swallowed.
    notice = _retrieval_shortfall_notice(results) or ""
    assert "시장 데이터 조회 1건 중 0건에서 자료를 확보했습니다." in notice
    assert "injected mart failure" not in notice


def test_f5_control_arm_a_healthy_mart_lane_reports_no_shortfall() -> None:
    """F5 (control arm): without the injection nothing is reported."""
    results = _build(_ok("mart")).execute(_plan(), session_id="r15-f5-control")

    assert all(result.status == "ok" for result in results)
    assert _retrieval_shortfall_notice(results) is None


def test_f3_an_injected_delay_is_reported_rather_than_read_as_absence() -> None:
    """F3 (injected arm): a slow mart lane must not read as "no data"."""

    def slow_mart(query, **_kwargs):
        time.sleep(3.0)
        return SourceResult(source="mart", query=query, status="ok")

    results = _build(slow_mart).execute(_plan(), session_id="r15-f3")

    mart = next(result for result in results if result.source == "mart")
    assert mart.status == "timeout"
    notice = _retrieval_shortfall_notice(results) or ""
    assert "조회 시간이 초과되어 이번 답변에 반영되지 않았습니다" in notice
    assert "per_tool_timeout" not in notice


def test_f3_control_arm_a_prompt_mart_lane_is_silent() -> None:
    """F3 (control arm): the same call without the delay reports nothing."""
    results = _build(_ok("mart")).execute(_plan(), session_id="r15-f3-control")

    mart = next(result for result in results if result.source == "mart")
    assert mart.status == "ok"
    assert _retrieval_shortfall_notice(results) is None
