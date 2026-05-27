from __future__ import annotations

from phase_zeta_runner.bundle_invariant_validator import validate_bundle_invariants
from phase_zeta_runner.config import RunnerConfig


def _config():
    return RunnerConfig.default_for_tests().validator


def test_rank_vs_raw_mismatch_detects_novosevenrt_scenario():
    bundle = {
        "brand_context": {"name": "헴리브라"},
        "market_views": [
            {
                "view_id": "ML.IQVIA.sales",
                "market_size": {"history": {"2025-Q4": {"raw_value": 23400000000.0}}},
                "target_brand_metric": {
                    "history": {"2025-Q4": {"raw_value": 10800000000.0, "rank": 1, "ms_pct": 46.1538}}
                },
                "competitors_top5": [
                    {"brand_name": "애드베이트", "history": {"2025-Q4": {"raw_value": 3000000000.0, "rank": 2, "ms_pct": 12.8205}}},
                    {"brand_name": "애디노베이트", "history": {"2025-Q4": {"raw_value": 2400000000.0, "rank": 3, "ms_pct": 10.2564}}},
                    {"brand_name": "노보세븐알티", "history": {"2025-Q4": {"raw_value": 1500000000.0, "rank": 1, "ms_pct": 80.41}}},
                    {"brand_name": "그린모노", "history": {"2025-Q4": {"raw_value": 1470000000.0, "rank": 4, "ms_pct": 6.2821}}},
                    {"brand_name": "진타솔로퓨즈", "history": {"2025-Q4": {"raw_value": 1400000000.0, "rank": 5, "ms_pct": 5.9829}}},
                ],
            }
        ],
    }

    result = validate_bundle_invariants(bundle, _config())

    assert not result["valid"]
    rank_violations = [v for v in result["violations"] if v["type"] == "rank_vs_raw_order_mismatch"]
    assert any(v["brand"] == "노보세븐알티" and v["expected_rank_by_raw"] == 4 for v in rank_violations)
    ms_violations = [v for v in result["violations"] if v["type"] == "ms_calculation_mismatch"]
    assert any(v["brand"] == "노보세븐알티" for v in ms_violations)


def test_valid_subset_with_target_below_top5_does_not_require_compact_rank():
    bundle = {
        "brand_context": {"name": "작은브랜드"},
        "market_views": [
            {
                "view_id": "ML.UBIST.sales",
                "market_size": {"history": {"2026-04": 1000.0}},
                "target_brand_metric": {
                    "history": {"2026-04": {"raw_value": 10.0, "rank": 20, "ms_pct": 1.0}}
                },
                "competitors_top5": [
                    {"brand_name": "A", "history": {"2026-04": {"raw_value": 300.0, "rank": 1, "ms_pct": 30.0}}},
                    {"brand_name": "B", "history": {"2026-04": {"raw_value": 200.0, "rank": 2, "ms_pct": 20.0}}},
                ],
            }
        ],
    }

    result = validate_bundle_invariants(bundle, _config())

    assert result["valid"]


def test_null_rank_rows_are_ignored_instead_of_crashing():
    bundle = {
        "brand_context": {"name": "순위없는브랜드"},
        "market_views": [
            {
                "view_id": "ML.UBIST.sales",
                "market_size": {"history": {"2026-04": 1000.0}},
                "target_brand_metric": {
                    "history": {"2026-04": {"raw_value": 10.0, "rank": None, "ms_pct": 1.0}}
                },
                "competitors_top5": [
                    {"brand_name": "A", "history": {"2026-04": {"raw_value": 300.0, "rank": 1, "ms_pct": 30.0}}},
                    {"brand_name": "B", "history": {"2026-04": {"raw_value": 200.0, "rank": None, "ms_pct": 20.0}}},
                ],
            }
        ],
    }

    result = validate_bundle_invariants(bundle, _config())

    assert result["valid"]


def test_ms_sum_exceeds_100_detected():
    bundle = {
        "brand_context": {"name": "타겟"},
        "market_views": [
            {
                "view_id": "ML.UBIST.sales",
                "market_size": {"history": {"2026-04": 1000.0}},
                "target_brand_metric": {"history": {"2026-04": {"raw_value": 600.0, "rank": 1, "ms_pct": 60.0}}},
                "competitors_top5": [
                    {"brand_name": "A", "history": {"2026-04": {"raw_value": 500.0, "rank": 2, "ms_pct": 50.0}}}
                ],
            }
        ],
    }

    result = validate_bundle_invariants(bundle, _config())

    assert not result["valid"]
    assert any(v["type"] == "ms_sum_exceeds_100" for v in result["violations"])
