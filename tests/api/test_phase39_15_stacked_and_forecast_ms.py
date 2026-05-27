from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause
from pipeline.scripts.etl import build_cache_deep_analysis


def _rank_row(name: str, value: float, rank: int, *, company: str | None = None) -> dict:
    return {
        "brand": name,
        "company": company or f"{name}사",
        "rank": rank,
        "raw_value": value,
        "ms": value,
    }


def test_stacked_trends_track_latest_target_top5_and_others() -> None:
    period_map = {}
    for year in range(2021, 2026):
        rows = [_rank_row(f"경쟁{i}", 100 - i, i) for i in range(1, 8)]
        rows.append(_rank_row("타겟", 10 + year - 2021, 20))
        period_map[f"{year}-12"] = rows

    stacked = build_cache_cause._stacked_ranking(
        period_map,
        label_key="brand",
        target_name="타겟",
        top_n=5,
    )

    brands = stacked["brands"]
    assert len(brands) == 7
    assert brands[0]["brand"] == "타겟"
    assert [brand["brand"] for brand in brands[1:6]] == ["경쟁1", "경쟁2", "경쟁3", "경쟁4", "경쟁5"]
    assert brands[6]["brand"] == "기타"
    assert brands[6]["is_others"] is True
    assert all([item["year"] for item in brand["yearly_values"]] == stacked["years"] for brand in brands)


def test_forecast_ms_series_uses_market_totals_and_matches_value_lengths() -> None:
    combo = {
        "history_periods": ["2025-Q1", "2025-Q2"],
        "forecast_periods": ["2025-Q3", "2025-Q4", "2026-Q1"],
        "brands": [
            {"brand": "A", "history_values": [10, 20], "forecast_values": [30, 40, 50]},
            {"brand": "B", "history_values": [30, 20], "forecast_values": [70, 60, 50]},
        ],
    }

    build_cache_deep_analysis._attach_forecast_ms_series(combo)

    a, b = combo["brands"]
    assert a["history_ms_pct"] == [25.0, 50.0]
    assert b["history_ms_pct"] == [75.0, 50.0]
    assert a["forecast_ms_pct"] == [30.0, 40.0, 50.0]
    assert b["forecast_ms_pct"] == [70.0, 60.0, 50.0]
    assert len(a["forecast_ms_pct"]) == len(a["forecast_values"])
