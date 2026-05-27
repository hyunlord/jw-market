import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))
from pipeline.scripts.etl.build_cache_cause import _level_top5_trend
from pipeline.scripts.etl.layer3_compute_general_v3 import safe_float


def _row(brand: str, recent_rank: int, value: float) -> dict:
    periods = [f"2025-{month:02d}" for month in range(1, 13)]
    history = {
        period: {"value": value, "raw_value": value, "rank": recent_rank, "ms": value}
        for period in periods
    }
    return {
        "brand_name": brand,
        "brand_key": brand,
        "company": "테스트",
        "metric_history": json.dumps(history, ensure_ascii=False),
        "by_dimension": json.dumps({"class": "A"}, ensure_ascii=False),
    }


def test_level_top5_others_residual_aligns_to_display_periods() -> None:
    """기타 must be residual over the same 10 display periods, not segment total."""
    periods = [f"2025-{month:02d}" for month in range(1, 13)]
    rows = [_row(f"브랜드{i}", i, 10.0) for i in range(1, 8)]
    analysis_levels = {
        "levels": ["Class"],
        "periods_monthly": periods,
        "data": {
            "Class": {
                "by_channel": {
                    "전체": [
                        {
                            "name": "A",
                            "recent_share_pct": 100.0,
                            "value_series": [70.0 for _ in periods],
                        }
                    ]
                }
            }
        },
    }

    trend = _level_top5_trend(
        analysis_levels,
        rows,
        "UBIST",
        target_name=None,
        include_all_options=True,
    )

    level_data = trend["by_level"]["Class"]
    value = level_data["values"][0]
    others = next(row for row in value["brands_in_value"] if row["brand"] == "기타")
    total_ms = sum(row["ms_series_10pt"][-1] for row in value["brands_in_value"])

    assert value["total_value"] == 70.0
    assert len(level_data["periods_10pt"]) == 10
    assert len(others["value_series_10pt"]) == 10
    assert others["value_series_10pt"][-1] == 20.0
    assert others["ms_series_10pt"][-1] == 28.5714
    assert 99.99 <= total_ms <= 100.01


def test_safe_float_keeps_numeric_fast_path_equivalent() -> None:
    assert safe_float(1.25) == 1.25
    assert safe_float("1,234.5") == 1234.5
    assert safe_float(True) == 0.0
