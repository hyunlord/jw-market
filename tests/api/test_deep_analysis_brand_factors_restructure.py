from __future__ import annotations

from pipeline.scripts.api import deep_analysis_brand_elements
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice


def test_build_brand_factors_groups_factors_and_strength_by_source() -> None:
    choices = tuple(
        BrandChoice(
            brand_key=brand,
            brand_name=brand,
            sales_rank=index,
            is_selected=index == 1,
        )
        for index, brand in enumerate(
            ("selected", "empty", "factor-only", "strength-only", "competitor-4", "competitor-5"),
            start=1,
        )
    )
    cached = {
        "selected": {
            "factors": {
                "iqvia": {"mfr_name_kor": ["JW"]},
                "ubist": {"seller": ["JW"]},
            }
        },
        "factor-only": {"factors": {"iqvia": {"molecule_desc": ["PITAVASTATIN"]}}},
        "strength-only": {"factors": {"ubist": {}}},
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
        },
        "strength-only": {
            "ubist": {
                "profile_display": {"headline": "UBIST"},
                "strength_items": [],
                "limitations": [],
            }
        },
    }

    items = deep_analysis_brand_elements.build_brand_factors(
        choices,
        selected_brand_key="selected",
        cached_elements_by_key=cached,
        selected_factors={},
        strength_by_source_by_key=source_strength,
    )

    assert len(items) == 6
    assert [item["role"] for item in items] == ["selected", *["competitor"] * 5]
    assert [item["rank"] for item in items] == [1, 2, 3, 4, 5, 6]
    selected = items[0]
    assert selected["iqvia"]["factors"]["values"]["mfr_name_kor"] == ["JW"]
    assert selected["iqvia"]["strength"] == {
        "profile_display": {"headline": "IQVIA"},
        "strength_items": ["growth"],
        "limitations": [],
    }
    assert "workflow_id" not in selected["iqvia"]["strength"]
    assert selected["ubist"]["strength"]["limitations"] == ["candidate 0건"]
    assert items[1]["iqvia"] == {}
    assert items[1]["ubist"] == {}
    assert items[2]["iqvia"]["strength"] == {}
    assert items[2]["ubist"] == {}
    assert items[3]["ubist"]["factors"] == {
        "available": False,
        "reason": "not_generated",
        "values": {
            "seller": [],
            "molecule_strength": [],
            "form": [],
            "route": [],
            "reimbursement": [],
        },
    }
    assert items[3]["iqvia"] == {}
    assert all("factors" not in item for item in items)
    assert all("strength" not in item for item in items)
    assert all("strength_by_source" not in item for item in items)
