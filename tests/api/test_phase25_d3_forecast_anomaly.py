from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "http://127.0.0.1:8013"
HTML_PATH = Path("docs/reference/jw_market_hardcoded_mockup_v3_4.html")


def get_api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def cause_payload(brand: str, *, view: str, source: str, measure: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(brand)
    return get_api(f"/api/cause/{encoded}?view={view}&source={source}&measure={measure}")["data"]


def _contains_key(obj: Any, key_name: str) -> bool:
    if isinstance(obj, dict):
        return any(key == key_name or _contains_key(value, key_name) for key, value in obj.items())
    if isinstance(obj, list):
        return any(_contains_key(value, key_name) for value in obj)
    return False


def test_d3_removes_brand_and_single_option_ox_gx_for_livalo() -> None:
    data = cause_payload("리바로", view="market_landscape", source="UBIST", measure="sales")
    by_level = data["level_top5_trend"]["by_level"]

    assert "Brand" not in by_level
    assert "Ox/Gx" not in by_level
    assert by_level
    assert all(len(payload.get("all_options") or []) > 1 for payload in by_level.values())


def test_deep_forecast_discloses_deterministic_history_only_method() -> None:
    payload = get_api(f"/api/deep-analysis/{urllib.parse.quote('리바로')}")
    forecast = payload["data"]["forecast"]

    assert forecast.get("method") == "deterministic_history_only_v0.9.1"
    assert forecast.get("disclaimer")
    assert forecast.get("is_statistical_model") is False
    assert forecast.get("backtest_available") is False


def test_anomaly_signals_are_removed_from_backend_and_frontend() -> None:
    payload = get_api(f"/api/deep-analysis/{urllib.parse.quote('리바로')}")
    html = HTML_PATH.read_text(encoding="utf-8")

    assert not _contains_key(payload.get("data"), "anomaly_signals")
    assert "최근 이상 변동" not in html
    assert "renderAnomalyBoxFromData" not in html


def test_phase25_extended_pipeline_passes() -> None:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase25_extended_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=360,
    )
    assert result.returncode == 0, result.stdout + result.stderr
