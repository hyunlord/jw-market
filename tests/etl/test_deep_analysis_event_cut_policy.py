from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.build_cache_deep_analysis import _events_spec_list


def _event(event_id: str, score: int) -> dict[str, object]:
    return {
        "id": event_id,
        "impact_score": score,
        "category": "capital",
        "category_label": "자본/경영",
    }


def test_chart_visibility_comes_from_versioned_cut_b_membership() -> None:
    payload = {
        "cut_a": [
            _event("legacy-80", 80),
            _event("rev5674-87", 87),
            _event("rev5674-88", 88),
            _event("list-only", 79),
        ],
        "cut_b": [
            _event("legacy-80", 80),
            _event("rev5674-88", 88),
        ],
    }

    events = _events_spec_list(payload)
    by_id = {event["id"]: event for event in events}

    assert by_id["legacy-80"]["on_chart"] is True
    assert by_id["rev5674-88"]["on_chart"] is True
    assert by_id["rev5674-87"]["on_chart"] is False
