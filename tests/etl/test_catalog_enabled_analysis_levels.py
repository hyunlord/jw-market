from __future__ import annotations

from copy import deepcopy

from pipeline.scripts.etl import build_cache_cause


def test_response_levels_emit_only_catalog_enabled_levels() -> None:
    market = {
        "ml_id": "ml_001",
        "analyze_class": 1,
        "analyze_molecule": 1,
        "analyze_dosage_form": 0,
        "analyze_strength_pack": 0,
        "analyze_nhi_type": 0,
        "analyze_ox_gx": 0,
    }

    levels = build_cache_cause._response_levels(market, "ml_001")

    assert levels == ["Class", "Molecule", "Brand"]


def test_response_levels_preserve_catalog_order_for_cd_market() -> None:
    market = {
        "cd_id": "cd_010",
        "analyze_class": 1,
        "analyze_molecule": 1,
        "analyze_dosage_form": 1,
        "analyze_strength_pack": 0,
        "analyze_nhi_type": 0,
        "analyze_ox_gx": 0,
    }

    levels = build_cache_cause._response_levels(market, "cd_010")

    assert levels == ["Class", "Molecule", "Brand", "제형/투여경로"]


def test_ml011_keeps_split_class_levels_and_portal_alias() -> None:
    market = {
        "ml_id": "ml_011",
        "analyze_class": 1,
        "analyze_molecule": 1,
        "analyze_dosage_form": 0,
        "analyze_strength_pack": 0,
        "analyze_nhi_type": 0,
        "analyze_ox_gx": 0,
    }
    payload = {
        "levels": build_cache_cause._response_levels(market, "ml_011"),
        "data": {
            "Class 1": {"segments": ["class-1"]},
            "Class 2": {"segments": ["class-2"]},
            "Molecule": {"segments": ["molecule"]},
            "Brand": {"segments": ["brand"]},
        },
    }

    result = build_cache_cause._ensure_split_class_alias(payload)

    assert result["levels"] == ["Class 1", "Class 2", "Molecule", "Brand"]
    assert result["data"]["Class"] == result["data"]["Class 2"]
    assert set(result["data"]) == {"Class", "Class 1", "Class 2", "Molecule", "Brand"}


def test_market_status_clone_preserves_enabled_level_bytes() -> None:
    enabled_data = {
        "Class": {"segments": ["전체", "A"], "by_channel": {"전체": [{"name": "A", "value": 1.0}]}},
        "Brand": {"segments": ["전체", "B"], "by_channel": {"전체": [{"name": "B", "value": 2.0}]}},
    }
    payload = {
        "levels": ["Class", "Brand"],
        "channels": ["전체"],
        "data": deepcopy(enabled_data),
    }

    result = build_cache_cause._analysis_level_market_status_by_channel(
        level_top5_trend={},
        analysis_levels=payload,
        rows=[],
        source="UBIST",
        channels=["전체"],
        include_all_options=False,
    )

    assert result["levels"] == ["Class", "Brand"]
    assert result["data"] == enabled_data
