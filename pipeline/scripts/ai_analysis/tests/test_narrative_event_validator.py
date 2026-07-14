from __future__ import annotations

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.narrative_event_validator import validate_narrative_events


def _config():
    return RunnerConfig.default_for_tests().validator


def test_all_cited_event_dates_match_bundle_events():
    parsed = {
        "phenomenon": {"title": "리바로 2026-05-04 임상 승인", "body": "", "bullets": ["2026년 5월 5일 후속 보도"]},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }
    bundle = {
        "event_bundle": {
            "events_brand_centric": [
                {"published_date": "2026-05-04", "brand_canonical": "리바로"},
                {"published_date": "2026-05-05", "brand_canonical": "리바로"},
            ]
        },
        "competitor_events": {"by_source": {}},
    }

    result = validate_narrative_events(parsed, bundle, _config())

    assert result["valid"]
    assert result["matched_dates"] == 2


def test_unmatched_event_date_is_reported():
    parsed = {
        "phenomenon": {"title": "리바로 2027-01-01 사건", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }
    bundle = {
        "event_bundle": {"events_brand_centric": [{"published_date": "2026-05-04", "brand_canonical": "리바로"}]},
        "competitor_events": {"by_source": {}},
    }

    result = validate_narrative_events(parsed, bundle, _config())

    assert not result["valid"]
    assert result["unmatched_dates"][0]["date"] == "2027-01-01"
