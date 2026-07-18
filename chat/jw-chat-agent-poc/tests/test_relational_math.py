from __future__ import annotations

from jw_chat_agent_poc.tools.query_layer.derived_growth import (
    terminal_streak as runtime_terminal_streak,
)
from jw_chat_agent_poc.tools.query_layer.derived_validation_math import (
    terminal_streak as validation_terminal_streak,
)


def test_terminal_streak_counts_transitions_not_points() -> None:
    values = [87.11, 84.93, 80.39]

    assert runtime_terminal_streak(values) == ("down", 2)
    assert validation_terminal_streak(tuple(values)) == ("down", 2)


def test_terminal_streak_stops_at_latest_reversal() -> None:
    values = [3.81, 3.75, 3.76]

    assert runtime_terminal_streak(values) == ("up", 1)
    assert validation_terminal_streak(tuple(values)) == ("up", 1)
