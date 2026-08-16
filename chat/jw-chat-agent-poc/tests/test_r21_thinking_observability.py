"""R21 - a turn must say which thinking level it asked the serving for.

Synthesis time was measured over 102 live turns as a linear function of one
thing: 6.5 ms per completion token, flat across every duration band, while
prompt size showed no relationship to it. 72-78% of those completion tokens are
reasoning tokens the user never sees, and the only control over them is
``thinking_level`` -- which nothing recorded. Without it, a fall in reasoning
tokens after an env change cannot be told apart from the variance an LLM has
anyway, and a later reader has no way to find out why answers got faster.

These pin the record, not the speed. Speed is a live claim and belongs to the
distribution comparison, not to a unit test.
"""
from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.v4.llm import _THINKING_LEVELS, thinking_observability
from jw_chat_agent_poc.service.v4.runtime import (
    _normalized_planner_usage,
    _normalized_synth_usage,
)


def _usage(completion: int, reasoning: int, text: int, prompt: int = 5163) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "completion_tokens_details": {"reasoning_tokens": reasoning, "text_tokens": text},
    }


def test_the_requested_level_is_recorded_beside_what_came_back():
    """Both halves, or the record proves nothing."""
    observed = thinking_observability("MEDIUM", _usage(8742, 7285, 1457))

    assert observed["requested_level"] == "MEDIUM"
    assert observed["completion_tokens"] == 8742
    assert observed["reasoning_tokens"] == 7285
    assert observed["text_tokens"] == 1457
    assert observed["reasoning_share"] == pytest.approx(0.8333, abs=1e-4)
    assert observed["measurement"] == "reported"


def test_the_share_is_what_makes_two_levels_comparable():
    """The live figure to beat: MEDIUM ran at 0.72-0.78 reasoning share."""
    medium = thinking_observability("MEDIUM", _usage(6887, 5405, 1482))
    low = thinking_observability("LOW", _usage(3200, 1700, 1500))

    assert medium["reasoning_share"] > low["reasoning_share"]
    # text_tokens is the half that must NOT move: it is the answer the user reads.
    assert low["text_tokens"] >= 1000, "a level change must not be read as an answer cut"


def test_a_turn_with_no_usage_says_so_rather_than_reporting_zeros_as_fact():
    observed = thinking_observability("LOW", {})

    assert observed["measurement"] == "unavailable"
    assert observed["reasoning_share"] == 0.0


def test_an_unrequested_level_is_named_not_left_empty():
    """``None`` and "we asked for nothing" must not read as missing data."""
    assert thinking_observability(None, _usage(10, 4, 6))["requested_level"] == "not_requested"


def test_zero_completion_tokens_does_not_divide_by_zero():
    assert thinking_observability("LOW", _usage(0, 0, 0))["reasoning_share"] == 0.0


def test_only_the_levels_the_serving_accepts_are_meaningful():
    assert _THINKING_LEVELS == {"LOW", "MEDIUM", "HIGH"}


# --------------------------------------------------------------------------
# the top-level trace fields, which is where an analyst actually reads this
# --------------------------------------------------------------------------


def test_synth_usage_carries_the_level_and_the_text_half():
    normalized = _normalized_synth_usage(
        {
            "usage": _usage(6887, 5405, 1482),
            "finish_reason": "stop",
            "thinking": thinking_observability("LOW", _usage(6887, 5405, 1482)),
        }
    )

    assert normalized["thinking_level"] == "LOW"
    assert normalized["output_tokens"] == 6887
    assert normalized["thinking_tokens"] == 5405
    assert normalized["text_tokens"] == 1482
    assert normalized["finish_reason"] == "stop"


def test_planner_usage_carries_the_level_too():
    normalized = _normalized_planner_usage(
        {"input_tokens": 2036, "output_tokens": 1435, "thinking_tokens": 687, "text_tokens": 748},
        thinking_observability("LOW", _usage(1435, 687, 748, prompt=2036)),
    )

    assert normalized["thinking_level"] == "LOW"
    assert normalized["text_tokens"] == 748


def test_a_trace_written_before_this_round_still_normalizes():
    """No migration: rows and outcomes that predate the field read as
    'not_reported' rather than crashing or claiming a level that was never asked."""
    normalized = _normalized_synth_usage({"usage": _usage(2218, 1610, 608), "finish_reason": "stop"})

    assert normalized["thinking_level"] == "not_reported"
    assert normalized["output_tokens"] == 2218

    planner = _normalized_planner_usage({"input_tokens": 820, "output_tokens": 1426, "thinking_tokens": 798})
    assert planner["thinking_level"] == "not_reported"
    assert planner["text_tokens"] == 0


def test_an_absent_usage_block_reports_not_applicable_across_the_board():
    assert _normalized_planner_usage(None)["thinking_level"] == "not_applicable"
    assert _normalized_synth_usage({})["thinking_level"] == "not_applicable"
