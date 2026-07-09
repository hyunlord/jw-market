from __future__ import annotations

import json
import math
from typing import Any

from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder


def _history(values: list[float]) -> str:
    return json.dumps({f"2025-{index + 1:02d}": {"raw_value": value, "ms": index + 1, "rank": index + 1} for index, value in enumerate(values)})


def _row(brand_key: str, brand_name: str, atc4_code: str, source: str, measure: str, value: float) -> dict[str, Any]:
    return {
        "id": f"{brand_key}-{atc4_code}-{source}-{measure}",
        "brand_key": brand_key,
        "brand_name": brand_name,
        "atc4_code": atc4_code,
        "atc4_desc": f"{atc4_code} 시장",
        "source": source,
        "measure": measure,
        "unit_label": "KRW",
        "metric_history": _history([value, value * 2]),
        "is_jw": brand_name == "타깃",
        "is_target": brand_name == "타깃",
    }


def test_select_groups_filters_by_brand_and_atc4_with_stable_order() -> None:
    # Given: grouped general mart rows for multiple brands and ATC4 markets.
    grouped = {
        ("b2", "B02"): [_row("b2", "브랜드2", "B02", "ubist", "sales", 1)],
        ("b1", "A01"): [_row("b1", "브랜드1", "A01", "ubist", "sales", 1)],
        ("b1", "A02"): [_row("b1", "브랜드1", "A02", "ubist", "sales", 1)],
    }

    # When: the caller limits the build to one brand and one ATC4.
    selected = general_builder.select_groups(grouped, brands={"브랜드1"}, atc4="A02", limit_groups=None)

    # Then: the selected build groups are deterministic and exact.
    assert [key for key, _rows in selected] == [("b1", "A02")]


def test_build_general_cache_row_uses_forecast_runner_payload_shape(monkeypatch) -> None:
    # Given: one target brand and one competitor in the same general ATC4 market.
    target = _row("target-key", "타깃", "A10N3", "ubist", "sales", 100)
    competitor = _row("competitor-key", "경쟁", "A10N3", "ubist", "sales", 200)
    market_rows = {("A10N3", "ubist", "sales"): [target, competitor]}

    def fake_build_entry(
        brand_row: dict[str, Any],
        *,
        target_brand: str,
        source: str,
        measure: str,
        forecast_steps_count: int,
    ) -> dict[str, Any]:
        return {
            "brand": brand_row["brand_name"],
            "is_target": brand_row["brand_name"] == target_brand,
            "is_jw": bool(brand_row["is_jw"]),
            "rank": 1,
            "history_values": [1.0, 2.0],
            "forecast_values": [3.0, 4.0],
            "forecast_model": {"name": "Mean", "selection_policy": "data_size_dispatch_v1"},
        }

    monkeypatch.setattr(general_builder, "build_forecast_brand_entry", fake_build_entry)
    monkeypatch.setattr(
        general_builder,
        "build_market_forecast",
        lambda _rows, _source, _steps: {"history_periods": ["2025-01", "2025-02"], "history_values": [3.0, 6.0], "forecast_values": [9.0, 12.0]},
    )
    monkeypatch.setattr(general_builder, "build_simulation_combo", lambda **_kwargs: {"ok": True})

    # When: the cache row is built.
    row = general_builder.build_general_cache_row(
        ("target-key", "A10N3"),
        [target],
        market_rows_by_combo=market_rows,
        brand_factors_by_brand={"타깃": {"atc": ["A10N3"], "ubist": {}, "iqvia": {}}},
    )

    # Then: the persisted row is keyed by brand+ATC4 and contains statistical forecast metadata.
    payload = json.loads(row.response_json)
    combo = payload["data"]["forecast"]["by_combo"]["UBIST.sales"]
    assert row.brand_key == "target-key"
    assert row.atc4_code == "A10N3"
    assert row.market_id == "general:A10N3"
    assert combo["brands"][0]["forecast_model"]["selection_policy"] == "data_size_dispatch_v1"
    assert payload["market_meta"]["cache_scope"] == "general"


def test_json_safe_replaces_non_finite_numbers() -> None:
    payload = {
        "ok": 1.25,
        "bad": float("nan"),
        "too_large_for_api_formatter": 1.0e120,
        "nested": [float("inf"), -float("inf"), {"keep": 3.0}],
    }

    safe = general_builder.json_safe(payload)

    assert safe == {
        "ok": 1.25,
        "bad": None,
        "too_large_for_api_formatter": None,
        "nested": [None, None, {"keep": 3.0}],
    }
    assert not math.isnan(safe["ok"])
