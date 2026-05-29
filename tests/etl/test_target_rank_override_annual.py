import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.build_cache_cause import TARGET_RANK_STATS_CACHE, _target_rank_overrides


def test_target_override_uses_annual_sum_not_latest_period():
    TARGET_RANK_STATS_CACHE.clear()
    rows = [
        {
            "brand": "리바로",
            "company": "JW",
            "is_jw": True,
            "metric_history": {"2025-01": 100, "2025-02": 100, "2025-12": 116},
        },
        {
            "brand": "경쟁A",
            "company": "A",
            "metric_history": {"2025-01": 50, "2025-02": 50, "2025-12": 50},
        },
    ]

    overrides = _target_rank_overrides(
        rows,
        label_key="brand",
        target_name="리바로",
        cache_key=("unit", "brand", "annual"),
    )

    assert overrides[2025]["value"] == 316
    assert overrides[2025]["rank"] == 1
    assert overrides[2025]["ms_pct"] == 67.8112


def test_target_override_uses_partial_year_sum():
    TARGET_RANK_STATS_CACHE.clear()
    rows = [
        {
            "brand": "리바로",
            "company": "JW",
            "is_jw": True,
            "metric_history": {"2026-01": 140, "2026-02": 140, "2026-03": 147, "2026-04": 144},
        },
        {
            "brand": "경쟁A",
            "company": "A",
            "metric_history": {"2026-01": 100, "2026-02": 100, "2026-03": 100, "2026-04": 100},
        },
    ]

    overrides = _target_rank_overrides(
        rows,
        label_key="brand",
        target_name="리바로",
        cache_key=("unit", "brand", "partial"),
    )

    assert overrides[2026]["value"] == 571
    assert overrides[2026]["rank"] == 1


def test_company_override_aggregates_member_brand_histories_annually():
    TARGET_RANK_STATS_CACHE.clear()
    rows = [
        {
            "brand": "리바로",
            "company": "JW",
            "is_jw": True,
            "metric_history": {"2025-01": 100, "2025-02": 100},
        },
        {
            "brand": "리바로젯",
            "company": "JW",
            "is_jw": True,
            "metric_history": {"2025-01": 80, "2025-02": 70},
        },
        {
            "brand": "경쟁A",
            "company": "A",
            "metric_history": {"2025-01": 120, "2025-02": 120},
        },
    ]

    overrides = _target_rank_overrides(
        rows,
        label_key="company",
        target_name="JW",
        cache_key=("unit", "company", "annual"),
    )

    assert overrides[2025]["value"] == 350
    assert overrides[2025]["rank"] == 1
    assert overrides[2025]["company"] == "JW"
