from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request

from pipeline.scripts.validation.phase28_no_hardcoding_pipeline import (
    FORECAST_METHOD,
    validate_ai_analysis,
    validate_events,
    validate_forecast,
    validate_no_anomaly,
    validate_simulation,
)


BASE_URL = "http://127.0.0.1:8013"


def _deep_data(brand: str = "리바로") -> dict:
    encoded = urllib.parse.quote(brand)
    with urllib.request.urlopen(f"{BASE_URL}/api/deep-analysis/{encoded}", timeout=30) as response:
        payload = json.load(response)
    assert payload.get("data"), payload
    return payload["data"]


def test_ai_analysis_is_empty_until_real_analysis_exists() -> None:
    data = _deep_data("리바로")
    assert validate_ai_analysis("리바로", data) == []
    assert data.get("ai_analysis") in ({}, None)


def test_events_do_not_use_legacy_mock_payloads() -> None:
    data = _deep_data("리바로")
    assert validate_events("리바로", data) == []


def test_forecast_keeps_phase24_deterministic_disclosure() -> None:
    data = _deep_data("리바로")
    assert validate_forecast("리바로", data) == []
    forecast = data.get("forecast") or {}
    assert forecast.get("method") == FORECAST_METHOD
    assert forecast.get("is_statistical_model") is False
    assert forecast.get("backtest_available") is False
    assert forecast.get("disclaimer")


def test_simulation_is_empty_until_generated_simulation_exists() -> None:
    data = _deep_data("리바로")
    assert validate_simulation("리바로", data) == []
    simulation = data.get("simulation") or {}
    assert (simulation.get("by_combo") or {}) == {}


def test_anomaly_signals_remain_removed_from_deep_analysis() -> None:
    data = _deep_data("리바로")
    assert validate_no_anomaly("리바로", data) == []


def test_phase28_no_hardcoding_pipeline_passes() -> None:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase28_no_hardcoding_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
