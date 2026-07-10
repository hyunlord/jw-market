from __future__ import annotations

from pipeline.scripts.api import deep_analysis_brand_elements
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice


def _choice(brand: str, rank: int | None, *, selected: bool = False) -> BrandChoice:
    return BrandChoice(
        brand_key=brand,
        brand_name=brand,
        sales_rank=rank,
        is_selected=selected,
    )


def test_build_brand_factors_keeps_independent_source_competitor_lists() -> None:
    choices_by_source = {
        "iqvia": (
            _choice("selected", 3, selected=True),
            _choice("iqvia-1", 1),
            _choice("iqvia-2", 2),
        ),
        "ubist": (
            _choice("selected", 2, selected=True),
            _choice("ubist-1", 1),
        ),
    }
    cached = {
        "selected": {
            "factors": {
                "iqvia": {"mfr_name_kor": ["JW"]},
                "ubist": {"seller": ["JW"]},
            }
        },
        "iqvia-1": {"factors": {"iqvia": {"molecule_desc": ["A"]}}},
        "iqvia-2": {"factors": {"iqvia": {"molecule_desc": ["B"]}}},
        "ubist-1": {"factors": {"ubist": {"seller": ["UBIST seller"]}}},
    }
    source_strength = {
        "selected": {
            "iqvia": {
                "profile_display": {"headline": "IQVIA"},
                "strength_items": ["growth"],
                "limitations": [],
                "workflow_id": 99,
            },
            "ubist": {
                "profile_display": {},
                "strength_items": [],
                "limitations": ["candidate 0건"],
            },
        }
    }

    factors = deep_analysis_brand_elements.build_brand_factors(
        choices_by_source,
        selected_brand_key="selected",
        cached_elements_by_key=cached,
        selected_factors={},
        strength_by_source_by_key=source_strength,
    )

    assert set(factors) == {"iqvia", "ubist"}
    assert [item["brand_key"] for item in factors["iqvia"]] == ["selected", "iqvia-1", "iqvia-2"]
    assert [item["rank"] for item in factors["iqvia"]] == [3, 1, 2]
    assert [item["brand_key"] for item in factors["ubist"]] == ["selected", "ubist-1"]
    assert [item["rank"] for item in factors["ubist"]] == [2, 1]
    assert factors["iqvia"][0]["role"] == "selected"
    assert factors["iqvia"][1]["role"] == "competitor"
    assert factors["iqvia"][0]["factors"]["values"]["mfr_name_kor"] == ["JW"]
    assert factors["iqvia"][0]["strength"]["strength_items"] == ["growth"]
    assert "workflow_id" not in factors["iqvia"][0]["strength"]
    assert factors["ubist"][0]["strength"]["limitations"] == ["candidate 0건"]
    assert factors["ubist"][1]["factors"]["values"]["seller"] == ["UBIST seller"]


def test_build_brand_factors_uses_selected_rank_null_when_source_market_is_unavailable() -> None:
    fallback = (_choice("selected", None, selected=True),)

    factors = deep_analysis_brand_elements.build_brand_factors(
        {"iqvia": fallback, "ubist": fallback},
        selected_brand_key="selected",
        cached_elements_by_key={
            "selected": {"factors": {"iqvia": {"mfr_name_kor": ["JW"]}, "ubist": {}}}
        },
        selected_factors={},
        strength_by_source_by_key={},
    )

    assert factors["iqvia"] == [
        {
            "brand": "selected",
            "brand_key": "selected",
            "role": "selected",
            "rank": None,
            "factors": {
                "available": True,
                "reason": None,
                "values": {
                    "mfr_name_kor": ["JW"],
                    "molecule_type": [],
                    "molecule_desc": [],
                    "pack_desc": [],
                    "strength": [],
                    "nhi_type": [],
                },
            },
            "strength": {},
        }
    ]
    assert factors["ubist"] == []
