from __future__ import annotations

import json

from pipeline.scripts.agent_refresh_weekly.finalize import finalize


def _write(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_finalize_combines_short_and_long_without_hiding_critical_failure(tmp_path) -> None:
    _write(
        tmp_path / "worklist.json",
        {
            "status": "route_plan_only",
            "routes": [
                {"brand_key": "jw", "canonical_brand_name": "리바로", "cohort": "jw"},
                {"brand_key": "tail", "canonical_brand_name": "기타", "cohort": "nonstrategic"},
            ],
            "diagnostics": {
                "density_worklist": {
                    "aliases": [["종근당자누비아", "종근당자누비아", "ml_003", "cd_003"]],
                    "excluded": [
                        {
                            "brand": "노보믹스",
                            "reason": "excluded_non_jw_market",
                            "source_event_count": 3,
                        }
                    ],
                }
            },
        },
    )
    _write(
        tmp_path / "short" / "run_manifest.json",
        {
            "brands": {
                "jw": {"status": "failed", "reason": "forced"},
                "tail": {"status": "validated"},
            },
            "cohort_metrics": {"jw": {"validated_over_reached": {"numerator": 0, "denominator": 1}}},
        },
    )
    _write(
        tmp_path / "long" / "run_manifest.json",
        {
            "brands": {"jw": {"status": "validated"}, "tail": {"status": "validated"}},
            "cohort_metrics": {"jw": {"validated_over_reached": {"numerator": 1, "denominator": 1}}},
        },
    )

    result = finalize(tmp_path, failure_threshold=5)

    assert result["verdict"] == "critical_failed"
    assert result["continue_execution"] is True
    assert result["failure_threshold"] == 5
    assert result["failures"][0]["analysis_variant"] == "short"
    assert result["excluded_non_jw_market"][0]["brand"] == "노보믹스"
    assert (tmp_path / "weekly_verdict.json").is_file()
