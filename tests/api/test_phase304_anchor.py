from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE_URL = "http://127.0.0.1:8013"


def _sim_brand(brand: str = "가드메트", combo: str = "UBIST.sales") -> dict:
    encoded = urllib.parse.quote(brand)
    with urllib.request.urlopen(f"{BASE_URL}/api/deep-analysis/{encoded}", timeout=30) as response:
        payload = json.load(response)
    return payload["data"]["simulation"]["by_combo"][combo]["by_brand"][brand]


def test_phase304_guardmet_ci_starts_at_history_endpoint() -> None:
    sim_brand = _sim_brand()
    history_last_period = sim_brand["history_periods"][-1]
    history_last_value = sim_brand["history_values"][-1]
    scenarios = sim_brand["scenarios"]

    assert sim_brand["forecast_periods"][0] == history_last_period
    assert sim_brand["forecast_values"][0] == history_last_value
    assert scenarios["base"]["values"][0] == history_last_value
    assert scenarios["upper"]["values"][0] == history_last_value
    assert scenarios["lower"]["values"][0] == history_last_value
    assert scenarios["upper"]["method"] == "selected_model_ci_upper_95_natural_with_funnel_floor"
    assert scenarios["lower"]["method"] == "selected_model_ci_lower_95_natural_with_funnel_floor"


def test_phase304_ci_width_is_zero_at_anchor_then_expands() -> None:
    sim_brand = _sim_brand()
    scenarios = sim_brand["scenarios"]
    upper = scenarios["upper"]["values"]
    lower = scenarios["lower"]["values"]

    assert upper[0] - lower[0] == 0
    assert upper[1] - lower[1] > 0
    assert upper[-1] - lower[-1] > upper[1] - lower[1]
