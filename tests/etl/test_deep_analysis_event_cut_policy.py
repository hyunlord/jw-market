from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.build_cache_deep_analysis import (
    EVENT_CHART_MAX,
    EVENT_CHART_MIN,
    _events_spec_list,
)


def _event(event_id: str, score: int) -> dict[str, object]:
    return {
        "id": event_id,
        "impact_score": score,
        "category": "capital",
        "category_label": "자본/경영",
    }


def test_chart_highlight_backfills_to_minimum_from_exposed_events() -> None:
    cut_a = [_event(f"e{index}", 90 - index) for index in range(8)]
    payload = {
        "cut_a": cut_a,
        "cut_b": [_event("e0", 90)],
    }

    events = _events_spec_list(payload)
    on_chart_ids = [event["id"] for event in events if event["on_chart"]]

    assert on_chart_ids == ["e0", "e1", "e2", "e3", "e4"]
    assert len(on_chart_ids) == EVENT_CHART_MIN


def test_chart_highlight_keeps_natural_count_between_bounds() -> None:
    cut_a = [_event(f"e{index}", 95 - index) for index in range(20)]
    payload = {
        "cut_a": cut_a,
        "cut_b": [_event(f"e{index}", 95 - index) for index in range(8)],
    }

    events = _events_spec_list(payload)
    on_chart_ids = [event["id"] for event in events if event["on_chart"]]

    assert on_chart_ids == [f"e{index}" for index in range(8)]


def test_chart_highlight_caps_at_maximum() -> None:
    cut_a = [_event(f"e{index}", 99 - index) for index in range(30)]
    payload = {
        "cut_a": cut_a,
        "cut_b": [_event(f"e{index}", 99 - index) for index in range(30)],
    }

    events = _events_spec_list(payload)
    on_chart_ids = [event["id"] for event in events if event["on_chart"]]

    assert len(on_chart_ids) == EVENT_CHART_MAX
    assert on_chart_ids == [f"e{index}" for index in range(EVENT_CHART_MAX)]


def test_chart_highlight_never_invents_beyond_exposed_events() -> None:
    cut_a = [_event("e0", 70), _event("e1", 60), _event("e2", 50)]
    payload = {"cut_a": cut_a, "cut_b": []}

    events = _events_spec_list(payload)
    on_chart_ids = [event["id"] for event in events if event["on_chart"]]

    assert on_chart_ids == ["e0", "e1", "e2"]
    assert all(event["on_chart"] for event in events)


def test_chart_highlight_stays_subset_of_exposed_list() -> None:
    cut_a = [_event(f"e{index}", 90 - index) for index in range(6)]
    payload = {
        "cut_a": cut_a,
        # cut_b references an event outside the exposed list: it must not
        # surface a highlight of its own, only exposed rows are flagged.
        "cut_b": [_event("not-exposed", 99)],
    }

    events = _events_spec_list(payload)
    exposed_ids = {event["id"] for event in events}
    on_chart_ids = {event["id"] for event in events if event["on_chart"]}

    assert "not-exposed" not in exposed_ids
    assert on_chart_ids <= exposed_ids
    assert len(on_chart_ids) == EVENT_CHART_MIN
