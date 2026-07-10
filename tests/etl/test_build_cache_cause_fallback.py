from __future__ import annotations

from pipeline.scripts.etl.build_cache_cause import _growth_ms_matrix


def test_growth_matrix_prefers_contribution_then_falls_back_to_momentum() -> None:
    payload = _growth_ms_matrix(
        [
            {
                "brand": "Contribution",
                "ms": 10.0,
                "growth_contribution": 7.0,
                "momentum_score": 99.0,
            },
            {
                "brand": "Fallback",
                "ms": 20.0,
                "growth_contribution": 0.0,
                "contribution_pct": 0.0,
                "momentum_score": 3.0,
            },
        ]
    )

    assert payload["data"][0]["contribution_pct"] == 7.0
    assert payload["data"][1]["contribution_pct"] == 3.0
