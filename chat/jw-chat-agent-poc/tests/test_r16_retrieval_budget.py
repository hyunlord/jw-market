"""R16 - the retrieval budgets have one reader each, and the wave bounds the answer.

The defect these fix in place: the wave budget existed twice as the literal
50.0, once in the executor and once at the first-wave call site. Raising the
environment override moved the first and the second clamped it straight back,
so the setting could only ever lower the budget, never raise it.
"""
from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.v4 import runtime as runtime_module


def test_defaults_are_the_values_this_round_settled_on():
    assert runtime_module._DEFAULT_PER_TOOL_TIMEOUT_S == 90.0
    assert runtime_module._DEFAULT_TOTAL_TIMEOUT_S == 50.0


def test_per_tool_no_longer_cuts_a_tool_before_the_wave_does():
    """Consistency: with per-tool at or above the wave, the wave is the only cut.

    This is the intended relationship, not an oversight. A tool cut five
    seconds before the wave ended lost work the wave was going to stop anyway.
    """
    assert runtime_module.per_tool_timeout_s() >= runtime_module.total_timeout_s()


def test_the_wave_budget_stays_inside_the_measured_answer_ceiling():
    """Ten measured answers put planner+synthesis+assembly at up to 77.4 s, and
    an earlier round saw synthesis alone at 69.5 s. Against the 130 s ceiling
    the wave may not exceed 52.6 s on the measured figure.
    """
    assert runtime_module.total_timeout_s() <= 52.6


@pytest.mark.parametrize("raw, expected", [("75", 75.0), ("12.5", 12.5)])
def test_the_wave_budget_can_now_be_raised_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv(runtime_module.TOTAL_TIMEOUT_ENV, raw)
    assert runtime_module.total_timeout_s() == expected


def test_the_per_tool_budget_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv(runtime_module.PER_TOOL_TIMEOUT_ENV, "60")
    assert runtime_module.per_tool_timeout_s() == 60.0


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-5"])
def test_an_unusable_override_is_reported_and_the_default_is_kept(monkeypatch, caplog, raw):
    monkeypatch.setenv(runtime_module.TOTAL_TIMEOUT_ENV, raw)
    with caplog.at_level("WARNING"):
        assert runtime_module.total_timeout_s() == 50.0
    if raw.strip():
        assert any(
            runtime_module.TOTAL_TIMEOUT_ENV in record.getMessage()
            for record in caplog.records
        ), "an ignored override must say so"


def test_the_first_wave_reads_the_same_budget_as_the_executor(monkeypatch):
    """Failure injection for the duplicated literal: raise the override and the
    first wave must move with it. Before the fix it was pinned at 50.0.
    """
    monkeypatch.setenv(runtime_module.TOTAL_TIMEOUT_ENV, "75")
    source = __import__("inspect").getsource(runtime_module.V4Runtime.answer)
    assert "min(50.0," not in source, "the wave budget must not be a second literal"
    assert runtime_module.total_timeout_s() == 75.0
