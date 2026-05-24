from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from typing import Any


BASE_URL = "http://127.0.0.1:8013"


def get_api(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def brand_names() -> list[str]:
    payload = get_api("/api/market-status")
    return [card["brand"] for card in payload["brand_cards"]]


def cause_payloads() -> list[tuple[str, str, str, dict[str, Any]]]:
    payloads: list[tuple[str, str, str, dict[str, Any]]] = []
    for brand in brand_names():
        encoded = urllib.parse.quote(brand)
        for view in ("market_landscape", "competitive_dynamics"):
            for source in ("UBIST", "IQVIA"):
                response = get_api(f"/api/cause/{encoded}?view={view}&source={source}&measure=sales")
                data = response.get("data")
                if isinstance(data, dict):
                    payloads.append((brand, view, source, data))
    return payloads


def assert_no_bad_numbers(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_bad_numbers(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_bad_numbers(item, f"{path}[{index}]")
        return
    if isinstance(value, float):
        assert not math.isnan(value), f"{path}: NaN"
        assert not math.isinf(value), f"{path}: Infinity"
    if isinstance(value, str):
        assert "NaN" not in value and "Infinity" not in value, f"{path}: {value}"


def test_all_view_source_target_zero_values_have_no_rank() -> None:
    """Phase 17 zero-rank protection must hold across every brand/view/source."""
    checked = 0
    zero_rows: list[tuple[str, str, str, int, float | int | None]] = []
    bad_rows: list[tuple[str, str, str, int, float | int | None]] = []

    for brand, view, source, data in cause_payloads():
        yearly = (data.get("brand_ranking_stacked") or {}).get("yearly") or []
        for year in yearly:
            target = next((row for row in year.get("rankings", []) if row.get("is_target")), None)
            if not target:
                continue
            checked += 1
            if target.get("value") == 0:
                row_id = (brand, view, source, year.get("year"), target.get("rank"))
                zero_rows.append(row_id)
                if target.get("rank") is not None:
                    bad_rows.append(row_id)

    assert checked >= 100
    assert zero_rows, "audit should include pre-launch or unavailable-source zero rows"
    assert bad_rows == []


def test_company_rankings_have_no_zero_value_rank_anomaly() -> None:
    """A.4 company ranking should not contain ranked zero-value rows or bad numeric values."""
    issues: list[tuple[str, str, str, int, str, Any]] = []

    for brand, view, source, data in cause_payloads():
        yearly = (data.get("company_ranking_stacked") or {}).get("yearly") or []
        for year in yearly:
            for row in year.get("rankings", []):
                if row.get("value") == 0 and row.get("rank") is not None:
                    issues.append((brand, view, source, year.get("year"), row.get("company"), row.get("rank")))
                assert_no_bad_numbers(row, f"{brand}.{view}.{source}.company_ranking.{year.get('year')}")

    assert issues == []


def test_cause_payloads_do_not_emit_nan_or_infinity() -> None:
    """Numeric edge cases must resolve to real numbers or null, never NaN/Infinity."""
    for brand, view, source, data in cause_payloads():
        assert_no_bad_numbers(data, f"{brand}.{view}.{source}")


def test_deep_analysis_forecast_values_remain_empty_without_hardcoded_simulation() -> None:
    """Forecast is not implemented yet, so edge-case audits must not find synthetic values."""
    for brand in brand_names():
        encoded = urllib.parse.quote(brand)
        payload = get_api(f"/api/deep-analysis/{encoded}")
        assert_no_bad_numbers(payload, brand)
        data = payload.get("data") or {}
        forecast = data.get("forecast") or {}

        for combo in (forecast.get("by_combo") or {}).values():
            for item in combo.get("brands") or []:
                assert item.get("forecast_values") in (None, [])


def test_phase17_winnerf_a_plus_regression_still_holds() -> None:
    payload = get_api("/api/market-status")
    card = next(card for card in payload["brand_cards"] if card["brand"] == "위너프A+")
    assert card["front"]["value_recent"] > 0

    encoded = urllib.parse.quote("위너프A+")
    cause = get_api(f"/api/cause/{encoded}?view=competitive_dynamics&source=IQVIA&measure=sales")
    year_2021 = next(year for year in cause["data"]["brand_ranking_stacked"]["yearly"] if year["year"] == 2021)
    target = next(row for row in year_2021["rankings"] if row["brand"] == "위너프A+")

    assert target["value"] == 0
    assert target["rank"] is None
