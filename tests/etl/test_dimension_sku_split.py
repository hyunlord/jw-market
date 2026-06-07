"""SKU-level dimension aggregation for strategic analysis levels."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause as cause
from pipeline.scripts.etl import layer3_compute_general_v3 as general
from pipeline.scripts.etl import ubist_channel_resolver
from pipeline.scripts import prototype_21_strategic_product_to_parquet as strategic_product


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


def test_level_top5_uses_dimension_rows_for_brand_values_and_keeps_overall_option():
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
    assert level_data["all_options"] == ["전체", "NHI", "NON-NHI"]
    assert [item["value"] for item in level_data["values"]] == ["전체", "NHI", "NON-NHI"]
    assert level_data["values"][0]["is_overall"] is True
    nhi_brand = level_data["values"][1]["brands_in_value"][0]
    assert nhi_brand["brand"] == "훼렉스"
    assert nhi_brand["value_recent"] == 120.0
    assert nhi_brand["value_series_10pt"] == [60.0, 120.0]


def test_brand_level_top5_keeps_overall_market_total_and_overall_option():
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
        "전체",
        "브랜드1",
        "브랜드2",
        "브랜드3",
        "브랜드4",
        "브랜드5",
    ]
    assert brand_level["values"][0]["is_overall"] is True


def test_analysis_levels_keep_overall_line_but_exclude_it_from_ms_options():
    rows = [
        {
            "brand_name": "브랜드A",
            "brand_key": "브랜드A",
            "company": "회사A",
            "metric_history": {"2025-Q1": {"raw_value": 100.0}},
            "by_dimension": {"class": "Class A"},
            "dimension_data": {
                "class": {"Class A": {"2025-Q1": {"raw_value": 100.0}}},
            },
        },
        {
            "brand_name": "브랜드B",
            "brand_key": "브랜드B",
            "company": "회사B",
            "metric_history": {"2025-Q1": {"raw_value": 50.0}},
            "by_dimension": {"class": "Class B"},
            "dimension_data": {
                "class": {"Class B": {"2025-Q1": {"raw_value": 50.0}}},
            },
        },
    ]

    analysis_levels = cause._build_analysis_levels_from_mart(
        rows=rows,
        source="IQVIA",
        market={"analyze_class": True},
        view_source_id="ml_test",
        target_name=None,
        fallback_level_top5={},
    )

    class_level = analysis_levels["data"]["Class"]
    line_segments = class_level["by_channel"]["전체"]
    overall = line_segments[0]
    assert overall["name"] == "전체"
    assert overall["is_overall"] is True
    assert overall["value_series"] == [150.0]
    assert "recent_share_pct" not in overall
    assert "series_pct" not in overall

    ms_segments = class_level["ms_by_channel"]["전체"]
    assert [segment["name"] for segment in ms_segments] == ["Class A", "Class B"]
    assert sum(segment["recent_share_pct"] for segment in ms_segments) == 100.0
    assert all(not segment.get("is_overall") for segment in ms_segments)


def test_analysis_level_market_status_overall_channel_matches_level_top5_payload():
    row = {
        "brand_name": "브랜드A",
        "brand_key": "브랜드A",
        "company": "회사A",
        "metric_history": {"2025-Q1": {"raw_value": 100.0}},
        "by_dimension": {"class": "Class A"},
        "dimension_data": {"class": {"Class A": {"2025-Q1": {"raw_value": 100.0}}}},
    }
    analysis_levels = {
        "levels": ["Class"],
        "periods_monthly": ["2025-Q1"],
        "data": {
            "Class": {
                "by_channel": {
                    "전체": [
                        {
                            "name": "전체",
                            "is_overall": True,
                            "value_series": [100.0],
                        },
                        {
                            "name": "Class A",
                            "recent_share_pct": 100.0,
                            "value_series": [100.0],
                        },
                    ],
                    "KHPA": [
                        {
                            "name": "전체",
                            "is_overall": True,
                            "value_series": [100.0],
                        },
                        {
                            "name": "Class A",
                            "recent_share_pct": 100.0,
                            "value_series": [100.0],
                        },
                    ],
                }
            }
        },
    }

    level_top5 = cause._level_top5_trend(
        analysis_levels,
        [row],
        "IQVIA",
        target_name=None,
        include_all_options=True,
    )
    clone_payload = cause._analysis_level_market_status_by_channel(
        level_top5_trend=level_top5,
        analysis_levels=analysis_levels,
        rows=[row],
        source="IQVIA",
        channels=["전체", "KHPA"],
        include_all_options=True,
    )

    assert clone_payload["by_channel"]["전체"] == level_top5
    assert clone_payload["by_channel"]["전체"] is not level_top5
    assert clone_payload["channels"] == ["전체", "KHPA"]
    assert [item["value"] for item in clone_payload["by_channel"]["전체"]["by_level"]["Class"]["values"]] == [
        "전체",
        "Class A",
    ]
    assert [item["value"] for item in clone_payload["ms_by_channel"]["전체"]["by_level"]["Class"]["values"]] == [
        "Class A"
    ]


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

    assert context["channels"] == ["전체", "상급종병", "종병", "병원", "의원", "보건소", "기타"]
    assert context["specialty_channels"] == ["전체", "종합병원 내분비", "의원 IGF"]
    assert rows[0]["__ubist_dual_channel_data"] == {
        "종합병원 내분비": {"2025-01": 10.0},
        "의원 IGF": {"2025-01": 5.0},
    }


def test_strategic_product_materializes_mi_master_molecule_and_preserves_raw_metadata():
    brand_row = {
        "name": "리바로젯",
        "merge_name": "리바로젯",
        "brand_id": "ml_006__livarozet",
        "ml_id": "ml_006",
        "cd_id": None,
        "class": "Statin + EZE",
        "molecule": "Statin/EZE",
        "dosage_form": "Oral",
        "strength_pack": None,
        "nhi_type": None,
        "ox_gx": "Combo",
        "fish_oil": None,
        "판매사": "JW중외제약",
        "제조사": "JW중외제약",
        "source_file_version": "test",
    }
    candidate = {
        "source_view": "IQVIA",
        "product_name": "LIVAROZET TAB 10/10MG",
        "pack_desc": "30T",
        "molecule": "atorvastatin calcium trihydrate (as atorvastatin), ezetimibe",
        "dosage_form": "Oral Solid Ordinary Film-Coated Tablets",
        "strength_pack": "10/10mg",
        "nhi_type": "NHI",
        "manufacturer": "JW중외제약",
    }

    record = strategic_product.product_record_from_candidate(
        brand_row=brand_row,
        context={},
        candidate=candidate,
        product_id="ml_006__livarozet__001",
        ingested_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )

    assert record["molecule"] == "Statin/EZE"
    assert record["molecule_raw"] == "atorvastatin calcium trihydrate (as atorvastatin), ezetimibe"
    assert record["dosage_form"] == "Oral"
    assert record["dosage_form_raw"] == "Oral Solid Ordinary Film-Coated Tablets"
    assert record["strength_pack"] == "10/10mg"
    assert record["nhi_type"] == "NHI"
