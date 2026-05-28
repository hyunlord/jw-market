"""Phase H cache_cause annual aggregation tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl.build_cache_cause import _annual_share_hhi, _stacked_ranking


def test_annual_aggregation():
    period_map = {
        "2025-01": [{"brand": "A", "sales": 100}, {"brand": "B", "sales": 50}],
        "2025-02": [{"brand": "A", "sales": 200}, {"brand": "B", "sales": 100}],
        "2026-01": [{"brand": "A", "sales": 50}, {"brand": "B", "sales": 200}],
    }
    result = _stacked_ranking(period_map, label_key="brand", target_name="A")
    assert result["years"] == [2025, 2026]
    assert result["series"]["A"] == [300, 50]
    assert result["series"]["B"] == [150, 200]


def test_top5_fixed_from_latest_year():
    period_map = {
        "2024-01": [
            {"brand": "A", "sales": 1000},
            {"brand": "B", "sales": 500},
            {"brand": "C", "sales": 300},
        ],
        "2025-01": [
            {"brand": "X", "sales": 2000},
            {"brand": "B", "sales": 100},
            {"brand": "C", "sales": 50},
        ],
    }
    result = _stacked_ranking(period_map, label_key="brand", target_name=None)
    assert "X" in result["top_brands"]
    assert "A" not in result["top_brands"]
    assert result["series"]["X"][0] == 0


def test_market_wide_ranking():
    period_map = {
        "2025-01": [
            {"brand": "A", "sales": 100},
            {"brand": "B", "sales": 200},
            {"brand": "C", "sales": 300},
            {"brand": "D", "sales": 400},
            {"brand": "E", "sales": 500},
            {"brand": "F", "sales": 600},
        ],
    }
    result = _stacked_ranking(period_map, label_key="brand", target_name="A")
    ranking = result["rankings_by_year"]["2025"]
    assert len(ranking) == 6
    assert ranking[0]["brand"] == "F" and ranking[0]["rank"] == 1
    assert ranking[5]["brand"] == "A" and ranking[5]["rank"] == 6


def test_other_sum():
    period_map = {
        "2025-01": [
            {"brand": "A", "sales": 100},
            {"brand": "B", "sales": 200},
            {"brand": "C", "sales": 300},
            {"brand": "D", "sales": 400},
            {"brand": "E", "sales": 500},
            {"brand": "F", "sales": 600},
            {"brand": "G", "sales": 700},
        ],
    }
    result = _stacked_ranking(period_map, label_key="brand", target_name="A")
    assert result["series"]["기타"] == [200]


def test_annual_share_hhi():
    period_map = {
        "2025-01": [{"brand": "A", "sales": 50}, {"brand": "B", "sales": 50}],
        "2025-02": [{"brand": "A", "sales": 50}, {"brand": "B", "sales": 50}],
    }
    result = _annual_share_hhi(period_map)
    assert len(result) == 1
    assert result[0]["year"] == 2025
    assert result[0]["hhi"] == 5000.0


def test_partial_year_aggregation():
    period_map = {
        "2025-12": [{"brand": "A", "sales": 1000}],
        "2026-01": [{"brand": "A", "sales": 100}],
        "2026-02": [{"brand": "A", "sales": 100}],
        "2026-03": [{"brand": "A", "sales": 100}],
        "2026-04": [{"brand": "A", "sales": 100}],
    }
    result = _stacked_ranking(period_map, label_key="brand", target_name="A")
    assert result["series"]["A"] == [1000, 400]
    assert result["period_count_by_year"] == {"2025": 1, "2026": 4}
