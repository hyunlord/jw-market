from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import strategic_runtime
from pipeline.scripts.api.routes import dynamic_market as dynamic_market_route
from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest


def test_strategic_runtime_uses_only_atc_narrowing_filters() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "ubist",
            "measure": "sales",
            "filters": {
                "atc4": ["C10A1"],
                "analysis_level": {
                    "ubist": {
                        "molecule_strength": ["10/10mg"],
                        "form": ["정"],
                        "route": ["경구"],
                        "reimbursement": ["급여"],
                    },
                    "iqvia": {},
                }
            },
        }
    )
    rows = [
        {
            "brand_key": "match",
            "brand_name": "매칭",
            "by_dimension": json.dumps(
                {
                    "atc4_code": "C10A1",
                    "strength_pack": "10/10mg",
                    "dosage_form": "정",
                    "route": "경구",
                    "nhi_type": "급여",
                    "manufacturer": "JW중외제약",
                }
            ),
        },
        {
            "brand_key": "miss",
            "brand_name": "미매칭",
            "by_dimension": json.dumps(
                {
                    "atc4_code": "C10C0",
                    "strength_pack": "20mg",
                    "dosage_form": "정",
                    "route": "경구",
                    "nhi_type": "급여",
                    "manufacturer": "경쟁사",
                }
            ),
        },
    ]

    filtered = strategic_runtime._filter_rows_by_analysis_level(
        rows=rows,
        source="ubist",
        analysis_level=dynamic_market_route._strategic_analysis_level_from_top_level_atc4(request),
    )

    assert [row["brand_key"] for row in filtered] == ["match"]


def test_strategic_runtime_matches_ubist_atc4_source_native_aliases() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "ubist",
            "measure": "sales",
            "filters": {"atc4": ["C10C0"]},
        }
    )
    rows = [
        {
            "brand_key": "source-native",
            "brand_name": "로수젯",
            "by_dimension": json.dumps({"atc4_code": "C10C"}),
        },
        {
            "brand_key": "other",
            "brand_name": "리바로",
            "by_dimension": json.dumps({"atc4_code": "C10A1"}),
        },
    ]

    filtered = strategic_runtime._filter_rows_by_analysis_level(
        rows=rows,
        source="ubist",
        analysis_level=dynamic_market_route._strategic_analysis_level_from_top_level_atc4(request),
    )

    assert [row["brand_key"] for row in filtered] == ["source-native"]


def test_strategic_runtime_matches_iqvia_atc4_source_native_aliases() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "iqvia",
            "measure": "sales",
            "filters": {"atc4": ["A10C1"]},
        }
    )
    rows = [
        {
            "brand_key": "source-native",
            "brand_name": "가드렛",
            "by_dimension": json.dumps({"atc4_code": "A10C1"}),
        },
        {
            "brand_key": "other",
            "brand_name": "기타",
            "by_dimension": json.dumps({"atc4_code": "A10B1"}),
        },
    ]

    filtered = strategic_runtime._filter_rows_by_analysis_level(
        rows=rows,
        source="iqvia_nsa",
        analysis_level=dynamic_market_route._strategic_analysis_level_from_top_level_atc4(request),
    )

    assert [row["brand_key"] for row in filtered] == ["source-native"]
