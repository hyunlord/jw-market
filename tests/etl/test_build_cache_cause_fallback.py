from __future__ import annotations

import math

from pipeline.scripts.etl.build_cache_cause import _growth_ms_matrix


def test_growth_matrix_falls_back_only_when_contribution_is_absent() -> None:
    payload = _growth_ms_matrix(
        [
            {
                "brand": "Contribution",
                "ms": 10.0,
                "growth_contribution": 7.0,
                "momentum_score": 99.0,
            },
            {
                "brand": "Missing",
                "ms": 20.0,
                "momentum_score": 3.0,
            },
            {
                "brand": "Zero",
                "ms": 30.0,
                "growth_contribution": 0.0,
                "contribution_pct": 0.0,
                "momentum_score": 4.0,
            },
            {
                "brand": "Negative zero",
                "ms": 40.0,
                "growth_contribution": -0.0,
                "contribution_pct": -0.0,
                "momentum_score": 5.0,
            },
        ]
    )

    assert payload["data"][0]["contribution_pct"] == 7.0
    assert payload["data"][1]["contribution_pct"] == 3.0
    assert payload["data"][2]["contribution_pct"] == 0.0
    assert payload["data"][3]["contribution_pct"] == -0.0
    assert math.copysign(1.0, payload["data"][3]["contribution_pct"]) == -1.0
