from __future__ import annotations

import phase_zeta_runner.run_pipeline as run_pipeline
from phase_zeta_runner.config import RunnerConfig


def test_full_validation_keeps_metric_stage_results_for_composer(monkeypatch):
    bundle = {"brand_context": {"name": "리바로"}, "market_views": []}
    parsed = {
        "phenomenon": {"title": "", "body": "", "bullets": []},
        "cause": {"title": "", "body": "", "bullets": []},
        "prediction": {"title": "", "body": "", "bullets": []},
        "recommendation": {"title": "", "body": "", "bullets": []},
    }

    monkeypatch.setattr(run_pipeline, "validate_bundle_against_mart", lambda *_: {"valid": True, "total_checks": 0, "matched": 0, "mismatched": [], "missing_in_mart": []})
    monkeypatch.setattr(run_pipeline, "validate_bundle_invariants", lambda *_: {"valid": True, "total_violations": 0, "violations": []})
    monkeypatch.setattr(run_pipeline, "validate_narrative_events", lambda *_: {"valid": False, "unmatched_dates": [{"date": "2027-01-01"}]})

    result = run_pipeline.run_full_validation(parsed, bundle, object(), RunnerConfig.default_for_tests())

    assert result.valid
    assert result.summary["layer4_valid"] is False
    assert "phenomenon" in result.stage_results
    assert result.to_dict()["layers"]["layer4_narrative_events"]["unmatched_dates"][0]["date"] == "2027-01-01"
