from __future__ import annotations

import pandas as pd

from pipeline.scripts.etl.layer3_compute_cd import build_cd_bridge, build_payloads_cd, level_breakdown
from pipeline.scripts.etl.layer3_compute_extended import compute_ei, compute_growth_contribution


def test_build_cd_bridge_filters_one_cd_market_and_uses_cd_schema_names() -> None:
    cd_brand = pd.DataFrame(
        [
            {"brand_id": "sb_001", "name": "A", "merge_name": None, "ml_id": "ml_001", "cd_id": "cd_001"},
            {"brand_id": "sb_002", "name": "B", "merge_name": "B merge", "ml_id": "ml_001", "cd_id": "cd_002"},
        ]
    )
    cd_product = pd.DataFrame(
        [
            {"product_id": "sp_001", "brand_id": "sb_001", "ml_id": "ml_001", "cd_id": "cd_001"},
            {"product_id": "sp_002", "brand_id": "sb_002", "ml_id": "ml_001", "cd_id": "cd_002"},
        ]
    )

    bridge = build_cd_bridge("cd_001", cd_brand, cd_product)

    assert bridge.to_dict("records") == [
        {
            "product_id": "sp_001",
            "cd_brand_id": "sb_001",
            "cd_brand_name": "A",
            "ml_id": "ml_001",
            "is_jw": False,
        }
    ]


def test_level_breakdown_counts_total_channel_and_channel_specialty() -> None:
    df = pd.DataFrame(
        [
            {"channel": None, "specialty": None},
            {"channel": "TH", "specialty": None},
            {"channel": "TH", "specialty": "Cardio"},
            {"channel": "GH", "specialty": "GI"},
        ]
    )

    breakdown = level_breakdown(df)

    assert breakdown.to_dict() == {
        "total": 1,
        "channel": 1,
        "channel_specialty": 2,
    }


def test_ratio_thresholds_null_small_denominators() -> None:
    assert compute_growth_contribution(35_123_453.82, -1_883.64) == (
        None,
        "gc_small_denominator",
    )
    assert compute_growth_contribution(20_000, 20_000) == (100.0, None)
    assert compute_ei(0.01, 0.0009) == (None, "ei_small_denominator")
    assert compute_ei(0.01, 0.001) == (1000.0, None)


def test_cd_payload_includes_denominator_warning_flags() -> None:
    df = pd.DataFrame(
        [
            {
                "cd_market_id": "cd_001",
                "raw_value": 100.0,
                "mom": None,
                "qoq": None,
                "yoy": None,
                "product_count": 1,
                "aggregation_level": "channel_specialty",
                "source_value_ubist": 100.0,
                "source_count_ubist": 1,
                "source_value_nsa": 0.0,
                "source_count_nsa": 0,
                "source_value_chso": 0.0,
                "source_count_chso": 0,
                "source_value_csd": 0.0,
                "source_count_csd": 0,
                "growth_contribution_warning": "gc_small_denominator",
                "ei_warning": "ei_small_denominator",
            }
        ]
    )

    payload = build_payloads_cd(df)[0]

    assert '"warnings":["gc_small_denominator","ei_small_denominator"]' in payload
