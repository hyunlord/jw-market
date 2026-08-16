"""R24 — every source gets asked before anything decides it has nothing to say.

Two guards used to remove lanes from a wave before their evidence could exist:
pruning dropped a source's queries when its supported-measure set did not intersect
the requested measure, and a satisfied answer-source quorum cancelled the lanes still
running six seconds in. Both are off by default now. These tests pin the defaults so a
future change has to say out loud that it is turning them back on.
"""

from __future__ import annotations

import time

from jw_chat_agent_poc.service.v4.contracts import SOURCE_NAMES, SourceResult
from jw_chat_agent_poc.service.v4.executor import (
    ParallelSourceExecutor,
    _quorum_early_exit_enabled,
)
from jw_chat_agent_poc.service.v4.synthesis_policy import prune_unsupported_source_queries

from tests.test_chat_v4_r12_7c import _plan


def test_pruning_is_off_by_default_so_no_source_is_dropped_unasked(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", raising=False)
    # requested measure is clinical_trials, which intersects neither mart nor nedrug
    # nor hira — exactly the shape that used to lose those three lanes.
    plan = _plan(
        hira=("리바로젯 처방 조제액 추이",),
        mart=("리바로젯 처방 조제액 추이",),
        nedrug=("리바로젯 허가 성분",),
    )

    pruned, trace = prune_unsupported_source_queries(plan)

    assert trace["applied"] is False
    assert trace["disabled"] is True
    assert trace["omitted"] == {}
    # The queries survive untouched — nothing was decided before a call was made.
    assert pruned.tool_queries.mart == plan.tool_queries.mart
    assert pruned.tool_queries.hira == plan.tool_queries.hira
    assert pruned.tool_queries.nedrug == plan.tool_queries.nedrug


def test_pruning_still_available_behind_the_switch(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", "1")
    plan = _plan(mart=("리바로젯 처방 조제액 추이",))

    _pruned, trace = prune_unsupported_source_queries(plan)

    assert trace["applied"] is True
    assert trace["omitted"]["mart"][0]["reason"] == "unsupported_measure"


def test_quorum_early_exit_switch_reads_the_env(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_V4_ANSWER_QUORUM_EARLY_EXIT", raising=False)
    assert _quorum_early_exit_enabled() is False

    for value in ("1", "true", "on", "yes", "YES"):
        monkeypatch.setenv("CHAT_V4_ANSWER_QUORUM_EARLY_EXIT", value)
        assert _quorum_early_exit_enabled() is True
    for value in ("0", "false", "off", "no", ""):
        monkeypatch.setenv("CHAT_V4_ANSWER_QUORUM_EARLY_EXIT", value)
        assert _quorum_early_exit_enabled() is False


def test_slow_lane_survives_a_satisfied_quorum_by_default(monkeypatch) -> None:
    """The lane the old default cancelled now returns its evidence."""

    monkeypatch.delenv("CHAT_V4_ANSWER_QUORUM_EARLY_EXIT", raising=False)

    def adapter(source: str, query: str) -> SourceResult:
        # hira answers immediately and satisfies the quorum; every other lane is slow
        # enough that the six-second cancel would have discarded it.
        time.sleep(0.01 if source == "hira" else 0.25)
        return SourceResult(source=source, query=query, status="ok", payload={"source": source})

    executor = ParallelSourceExecutor(
        adapters={name: (lambda query, source=name: adapter(source, query)) for name in SOURCE_NAMES},
        per_tool_timeout_s=2.0,
        total_timeout_s=2.0,
    )

    outcome = executor.execute_with_trace(
        _plan(),
        session_id="session-r24-no-early-exit",
        answer_sources=("hira",),
        soft_deadline_s=0.06,
    )

    by_source = {result.source: result for result in outcome.results}
    assert by_source["patent"].status == "ok", "the slow lane must not be cancelled"
    assert not any(
        item.notice == "정답 근거 도착 후 soft deadline으로 미포함" for item in outcome.results
    )
    assert outcome.trace["quorum_fired"] is False
    assert outcome.trace["quorum_early_exit_enabled"] is False
