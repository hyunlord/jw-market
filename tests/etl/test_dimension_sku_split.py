"""SKU-level dimension aggregation for strategic analysis levels."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause as cause
from pipeline.scripts.etl import layer3_compute_general_v3 as general


def test_analysis_level_uses_dimension_data_series_instead_of_joined_brand_value():
    row = {
        "brand_name": "페린젝트",
        "by_dimension": {"nhi_type": "NHI | NON-NHI"},
        "metric_history": {
            "2025-Q1": {"raw_value": 100.0},
            "2025-Q2": {"raw_value": 200.0},
        },
        "dimension_data": {
            "nhi_type": {
                "NHI": {
                    "2025-Q1": {"raw_value": 60.0},
                    "2025-Q2": {"raw_value": 120.0},
                },
                "NON-NHI": {
                    "2025-Q1": {"raw_value": 40.0},
                    "2025-Q2": {"raw_value": 80.0},
                },
            }
        },
    }

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="비/급여",
        periods=["2025-Q1", "2025-Q2"],
        source="IQVIA",
        channel="전체",
        target_name=None,
        top_n=None,
    )

    by_name = {segment["name"]: segment for segment in segments}
    assert set(by_name) == {"NHI", "NON-NHI"}
    assert by_name["NHI"]["value_series"] == [60.0, 120.0]
    assert by_name["NON-NHI"]["value_series"] == [40.0, 80.0]
    assert by_name["NHI"]["series_pct"] == [60.0, 60.0]
    assert by_name["NON-NHI"]["series_pct"] == [40.0, 40.0]


def test_build_dimensional_history_preserves_plus_compound_as_single_label():
    frame = pd.DataFrame(
        [
            {"period_yyyymm": "2025-Q1", "molecule": "METFORMIN+SITAGLIPTIN", "raw_value": 10.0},
            {"period_yyyymm": "2025-Q1", "molecule": "METFORMIN", "raw_value": 5.0},
            {"period_yyyymm": "2025-Q2", "molecule": "METFORMIN+SITAGLIPTIN", "raw_value": 20.0},
            {"period_yyyymm": "2025-Q2", "molecule": "METFORMIN", "raw_value": 8.0},
        ]
    )

    history = general.build_dimensional_history(frame, "molecule", ["2025-Q1", "2025-Q2"])

    assert "METFORMIN+SITAGLIPTIN" in history
    assert "METFORMIN" in history
    assert "SITAGLIPTIN" not in history
    assert history["METFORMIN+SITAGLIPTIN"]["2025-Q2"]["raw_value"] == 20.0


def test_channel_level_segments_use_dimension_channel_data_without_duplication():
    row = {
        "brand_name": "페린젝트",
        "by_dimension": {"nhi_type": "NHI | NON-NHI"},
        "channel_data": {
            "KHPA": {
                "2025-Q1": {"raw_value": 60.0},
                "2025-Q2": {"raw_value": 120.0},
            },
            "KCPA": {
                "2025-Q1": {"raw_value": 40.0},
                "2025-Q2": {"raw_value": 80.0},
            },
        },
        "dimension_channel_data": {
            "nhi_type": {
                "NHI": {
                    "KHPA": {
                        "2025-Q1": {"raw_value": 60.0},
                        "2025-Q2": {"raw_value": 120.0},
                    }
                },
                "NON-NHI": {
                    "KCPA": {
                        "2025-Q1": {"raw_value": 40.0},
                        "2025-Q2": {"raw_value": 80.0},
                    }
                },
            }
        },
    }

    segments = cause._segment_rows_for_level(
        rows=[row],
        level="비/급여",
        periods=["2025-Q1", "2025-Q2"],
        source="IQVIA",
        channel="KHPA",
        target_name=None,
        top_n=None,
    )

    assert segments == [
        {
            "name": "NHI",
            "rank": 1,
            "recent_share_pct": 100.0,
            "series_pct": [100.0, 100.0],
            "value_series": [60.0, 120.0],
        }
    ]
