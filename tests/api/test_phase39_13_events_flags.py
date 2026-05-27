from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.build_cache_deep_analysis import _apply_event_cut_flags


def _event(idx: int, score: int) -> dict:
    return {
        "id": f"event-{idx}",
        "date": f"2026-05-{idx:02d}",
        "impact_score": score,
    }


def test_event_cut_flags_apply_minimums_and_sort_by_score() -> None:
    events = [_event(i, score) for i, score in enumerate([20, 10, 90, 80, 70, 40, 35], start=1)]

    flagged = _apply_event_cut_flags(events)

    assert [event["impact_score"] for event in flagged] == [90, 80, 70, 40, 35, 20, 10]
    assert sum(event["on_list"] for event in flagged) == 7
    assert sum(event["on_chart"] for event in flagged) == 5
    assert all(not event["on_chart"] or event["on_list"] for event in flagged)


def test_event_cut_flags_cap_list_and_chart_counts() -> None:
    events = [_event(i, 100 - i) for i in range(60)]

    flagged = _apply_event_cut_flags(events)

    assert len(flagged) == 50
    assert sum(event["on_list"] for event in flagged) == 50
    assert sum(event["on_chart"] for event in flagged) == 15
    assert all(not event["on_chart"] or event["on_list"] for event in flagged)
