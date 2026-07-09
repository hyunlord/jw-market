from __future__ import annotations

import json

import pytest

from pipeline.scripts.etl.cache_deep_analysis_brand_factors import (
    CacheBrandFactorsError,
    build_brand_factor_map,
    dump_brand_factors,
    empty_brand_factors,
    quote_ident,
)
from pipeline.scripts.etl import build_cache_deep_analysis


def test_build_brand_factor_map_projects_requested_contract_keys() -> None:
    factors = build_brand_factor_map(
        brands=["리바로젯", "원천없음"],
        atc_rows=[
            {"brand_name": "리바로젯", "atc4_code": "C10C"},
            {"brand_name": "리바로젯", "atc4_code": "C10C0"},
            {"brand_name": "리바로젯", "atc4_code": "C10C"},
        ],
        dimension_rows=[
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "seller", "dimension_value": "JW중외제약"},
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "form", "dimension_value": "정제"},
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "route", "dimension_value": "내복"},
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "reimbursement", "dimension_value": "급여"},
            {
                "brand_name": "리바로젯",
                "source": "ubist",
                "dimension_type": "molecule_strength",
                "dimension_value": "ezetimibe 10㎎",
            },
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "mfr", "dimension_value": "제이더블유중외제약"},
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "molecule_type", "dimension_value": "COMBINE"},
            {
                "brand_name": "리바로젯",
                "source": "iqvia_nsa",
                "dimension_type": "molecule_desc",
                "dimension_value": "EZETIMIBE+PITAVASTATIN",
            },
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "pack", "dimension_value": "TAB 30"},
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "strength", "dimension_value": "2MG"},
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "nhi", "dimension_value": "NHI"},
        ],
    )

    assert factors["리바로젯"] == {
        "atc": ["C10C", "C10C0"],
        "ubist": {
            "seller": ["JW중외제약"],
            "molecule_strength": ["ezetimibe 10㎎"],
            "form": ["정제"],
            "route": ["내복"],
            "reimbursement": ["급여"],
        },
        "iqvia": {
            "mfr_name_kor": ["제이더블유중외제약"],
            "molecule_type": ["COMBINE"],
            "molecule_desc": ["EZETIMIBE+PITAVASTATIN"],
            "pack_desc": ["TAB 30"],
            "strength": ["2MG"],
            "nhi_type": ["NHI"],
        },
    }
    assert factors["원천없음"] == empty_brand_factors()


def test_dump_brand_factors_is_valid_json_with_empty_default() -> None:
    assert json.loads(dump_brand_factors(None)) == empty_brand_factors()


def test_quote_ident_rejects_unsafe_identifier() -> None:
    with pytest.raises(CacheBrandFactorsError):
        quote_ident("cache_deep_analysis;DROP")


def test_dump_brand_strength_uses_public_route_shape() -> None:
    payload = build_cache_deep_analysis.dump_brand_strength(
        {
            "available": True,
            "profile_display": {"headline": "strong"},
            "strength_items": [{"axis": "growth"}],
            "limitations": [],
            "meta": {"generated_at": "2026-07-05 13:32:16", "workflow_rev": 5365},
        }
    )

    assert json.loads(payload) == {
        "available": True,
        "profile_display": {"headline": "strong"},
        "strength_items": [{"axis": "growth"}],
        "limitations": [],
        "meta": {"generated_at": "2026-07-05 13:32:16", "workflow_rev": 5365},
    }


def test_parse_brand_strength_row_rejects_invalid_summary() -> None:
    assert build_cache_deep_analysis._parse_brand_strength_row({"strength_summary_json": "not-json"}) is None


def test_full_mode_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["build_cache_deep_analysis.py", "--mode", "full"])

    with pytest.raises(SystemExit, match="--confirm-full"):
        build_cache_deep_analysis.main()
