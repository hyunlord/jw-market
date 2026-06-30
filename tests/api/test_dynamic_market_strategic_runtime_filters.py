from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import strategic_runtime
from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest


def test_strategic_runtime_filters_ubist_sidecar_aliases() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "ubist",
            "measure": "sales",
            "filters": {
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
        analysis_level=request.filters.analysis_level,
    )

    assert [row["brand_key"] for row in filtered] == ["match"]
