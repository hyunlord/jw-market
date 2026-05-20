from __future__ import annotations

import pandas as pd

from pipeline.scripts.etl.layer3_compute_cd import build_cd_bridge, level_breakdown


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
