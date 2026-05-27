from __future__ import annotations

import json
from datetime import datetime

from pipeline.scripts.api.routes import deep_analysis as deep_route


def _brand(name: str, forecast_len: int) -> dict:
    return {
        "brand": name,
        "history_values": list(range(22)),
        "forecast_values": list(range(forecast_len)),
        "forecast_ms_pct": list(range(forecast_len)),
        "forecast_intervals": {
            "ci_upper_95": list(range(forecast_len)),
            "ci_lower_95": list(range(forecast_len)),
            "upper_horizon_adaptive": list(range(forecast_len)),
            "lower_horizon_adaptive": list(range(forecast_len)),
            "upper_95_natural": list(range(forecast_len)),
            "lower_95_natural": list(range(forecast_len)),
            "scalar_metadata": "kept",
        },
    }


def test_deep_analysis_serves_one_year_forecast_without_slicing_history(monkeypatch) -> None:
    payload = {
        "brand": "위너프A+",
        "data": {
            "forecast": {
                "by_combo": {
                    "IQVIA.sales": {
                        "period_unit": "분기",
                        "history_periods": [f"202{i}-Q1" for i in range(22)],
                        "forecast_periods": [f"203{i}-Q1" for i in range(41)],
                        "brands": [_brand("위너프A+", 41)],
                    },
                    "UBIST.sales": {
                        "period_unit": "월",
                        "history_periods": [f"2020-{i:02d}" for i in range(1, 61)],
                        "forecast_periods": [f"2030-{i:02d}" for i in range(1, 122)],
                        "brands": [_brand("위너프A+", 121)],
                    },
                }
            }
        },
    }

    def fake_fetch_one(query: str, params: list[str]) -> dict | None:
        if "cache_deep_analysis_ai_analysis" in query:
            return None
        return {"response_json": json.dumps(payload, ensure_ascii=False), "updated_at": datetime(2026, 5, 27)}

    monkeypatch.setattr(deep_route.db, "fetch_one", fake_fetch_one)

    response = deep_route.deep_analysis("위너프A+")
    combos = response["data"]["forecast"]["by_combo"]

    iqvia = combos["IQVIA.sales"]
    assert len(iqvia["history_periods"]) == 22
    assert len(iqvia["forecast_periods"]) == 4
    brand = iqvia["brands"][0]
    assert len(brand["forecast_values"]) == 4
    assert len(brand["forecast_ms_pct"]) == 4
    assert brand["forecast_intervals"]["scalar_metadata"] == "kept"
    for key in (
        "ci_upper_95",
        "ci_lower_95",
        "upper_horizon_adaptive",
        "lower_horizon_adaptive",
        "upper_95_natural",
        "lower_95_natural",
    ):
        assert len(brand["forecast_intervals"][key]) == 4

    ubist = combos["UBIST.sales"]
    assert len(ubist["history_periods"]) == 60
    assert len(ubist["forecast_periods"]) == 12
    brand = ubist["brands"][0]
    assert len(brand["forecast_values"]) == 12
    assert len(brand["forecast_ms_pct"]) == 12
    for key in (
        "ci_upper_95",
        "ci_lower_95",
        "upper_horizon_adaptive",
        "lower_horizon_adaptive",
        "upper_95_natural",
        "lower_95_natural",
    ):
        assert len(brand["forecast_intervals"][key]) == 12
