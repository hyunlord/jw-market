from __future__ import annotations

from pipeline.scripts.agent3.profile_provider import MoleculeRow, build_profile


def test_profile_prefers_recode_and_keeps_raw_values() -> None:
    profile = build_profile(
        brand_name="리바로젯",
        general_rows=[
            {
                "brand_key": "리바로젯",
                "brand_name": "리바로젯",
                "source": "ubist",
                "atc4_code": "C10C",
                "raw_value_history": {"2026-03": 100.0, "2026-04": 120.0},
                "dimension_data": {},
            },
            {
                "brand_key": "리바로젯",
                "brand_name": "리바로젯",
                "source": "iqvia_nsa",
                "atc4_code": "C10C0",
                "raw_value_history": {"2025-Q4": 80.0, "2026-Q1": 90.0},
                "dimension_data": {"nhi_type": {"NHI": {"2026-Q1": {"raw_value": 90.0}}}},
            },
        ],
        strategic_rows=[
            {
                "source": "ubist",
                "overlay_data": {
                    "class": "Statin/EZE",
                    "molecule": "PTV/EZE",
                    "strength_pack": "2/10mg | 4/10mg",
                    "ox_gx": "Ox",
                },
            }
        ],
        molecule_rows=[
            MoleculeRow("리바로젯", "any", "Ezetimibe", 2, True),
            MoleculeRow("리바로젯", "any", "Pitavastatin", 2, True),
        ],
    )

    assert profile["brand"] == "리바로젯"
    assert profile["class_recode"] == "Statin/EZE"
    assert profile["molecule_recode"] == "PTV/EZE"
    assert profile["molecule_raw"] == ["Ezetimibe", "Pitavastatin"]
    assert profile["molecule_type"] == "combination"
    assert profile["strength_pack_recode"] == "2/10mg | 4/10mg"
    assert profile["nhi_type_raw"] == ["NHI"]
    assert profile["latest"]["ubist"]["period"] == "2026-04"
    assert profile["latest"]["iqvia_nsa"]["period"] == "2026-Q1"


def test_profile_handles_missing_data_gracefully() -> None:
    profile = build_profile(
        brand_name="빈브랜드",
        general_rows=[],
        strategic_rows=[],
        molecule_rows=[],
    )

    assert profile["brand"] == "빈브랜드"
    assert profile["sources"] == []
    assert profile["molecule_raw"] == []
    assert profile["molecule_type"] is None
    assert profile["latest"] == {}
