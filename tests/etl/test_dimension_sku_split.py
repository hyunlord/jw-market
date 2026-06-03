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
from pipeline.scripts.etl import ubist_channel_resolver


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


def test_level_top5_uses_dimension_rows_for_brand_values_and_hides_overall_share_option():
    row = {
        "brand_name": "훼렉스",
        "brand_key": "훼렉스",
        "company": "JW",
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
    analysis_levels = {
        "levels": ["비/급여"],
        "periods_quarterly": ["2025-Q1", "2025-Q2"],
        "data": {
            "비/급여": {
                "by_channel": {
                    "전체": [
                        {
                            "name": "전체",
                            "is_overall": True,
                            "recent_share_pct": 100.0,
                            "value_series": [100.0, 200.0],
                        },
                        {
                            "name": "NHI",
                            "recent_share_pct": 60.0,
                            "value_series": [60.0, 120.0],
                        },
                        {
                            "name": "NON-NHI",
                            "recent_share_pct": 40.0,
                            "value_series": [40.0, 80.0],
                        },
                    ]
                }
            }
        },
    }

    trend = cause._level_top5_trend(
        analysis_levels,
        [row],
        "IQVIA",
        target_name="훼렉스",
        include_all_options=True,
    )

    level_data = trend["by_level"]["비/급여"]
    assert level_data["total_market_value"] == 200.0
    assert level_data["all_options"] == ["NHI", "NON-NHI"]
    assert [item["value"] for item in level_data["values"]] == ["NHI", "NON-NHI"]
    nhi_brand = level_data["values"][0]["brands_in_value"][0]
    assert nhi_brand["brand"] == "훼렉스"
    assert nhi_brand["value_recent"] == 120.0
    assert nhi_brand["value_series_10pt"] == [60.0, 120.0]


def test_brand_level_top5_keeps_overall_market_total_while_hiding_overall_option():
    rows = [
        {
            "brand_name": f"브랜드{i}",
            "brand_key": f"브랜드{i}",
            "company": f"회사{i}",
            "metric_history": {"2025-Q4": {"raw_value": value}},
            "by_dimension": {},
        }
        for i, value in enumerate([60.0, 50.0, 40.0, 30.0, 20.0, 10.0], start=1)
    ]

    analysis_levels = cause._build_analysis_levels_from_mart(
        rows=rows,
        source="IQVIA",
        market={},
        view_source_id="ml_test",
        target_name=None,
        fallback_level_top5={},
    )
    trend = cause._level_top5_trend(
        analysis_levels,
        rows,
        "IQVIA",
        target_name=None,
        include_all_options=True,
    )

    brand_level = trend["by_level"]["Brand"]
    assert brand_level["total_market_value"] == 210.0
    assert [item["value"] for item in brand_level["values"]] == [
        "브랜드1",
        "브랜드2",
        "브랜드3",
        "브랜드4",
        "브랜드5",
    ]


def test_target_customer_competition_copy_matches_level_top5_payload():
    level_top5 = {
        "available_levels": [{"key": "Class", "label": "Class"}],
        "default_level": "Class",
        "by_level": {"Class": {"values": [{"value": "A"}]}},
    }

    copied = cause._copy_level_top5_to_target_customer(level_top5)

    assert copied == level_top5
    assert copied is not level_top5


def test_ubist_resolver_returns_screen_facility_channels_and_preserves_specialty_data(monkeypatch):
    monkeypatch.setattr(
        ubist_channel_resolver,
        "_load_market_raw_totals",
        lambda brand_names, measure: (
            {
                "가드메트": {
                    "종합병원 내분비": {"2025-01": 10.0},
                    "의원 IGF": {"2025-01": 5.0},
                }
            },
                {"GH Endo": 10.0, "CL IGF": 5.0},
        ),
    )

    rows = [{"brand_name": "가드메트"}]
    context = ubist_channel_resolver.resolve_market_channels(
        rows=rows,
        market={"target_ubist_1": "GH Endo"},
        measure="sales",
    )

    assert context["channels"] == ["전체", "상급종병", "종병", "병원", "의원/보건소"]
    assert context["specialty_channels"] == ["전체", "종합병원 내분비", "의원 IGF"]
    assert rows[0]["__ubist_dual_channel_data"] == {
        "종합병원 내분비": {"2025-01": 10.0},
        "의원 IGF": {"2025-01": 5.0},
    }
