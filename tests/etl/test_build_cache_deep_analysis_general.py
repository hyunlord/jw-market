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
        market_forecasts_by_combo={
            ("A10N3", "ubist", "sales"): {"history_periods": ["2025-01", "2025-02"], "history_values": [3.0, 6.0], "forecast_values": [9.0, 12.0]}
        },
        selected_entries_by_group_combo={
            (("target-key", "A10N3"), "UBIST.sales"): [
                fake_build_entry(target, target_brand="", source="UBIST", measure="sales", forecast_steps_count=120),
                fake_build_entry(competitor, target_brand="", source="UBIST", measure="sales", forecast_steps_count=120),
            ]
        },
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


def test_build_batch_rows_reuses_market_and_brand_forecasts_within_same_atc4(monkeypatch) -> None:
    # Given: two target brands in the same ATC4 market share one source/measure
    # market, and both select the same three Top-N rows in different target order.
    target_one = _row("target-1", "타깃1", "A10N3", "ubist", "sales", 100)
    target_two = _row("target-2", "타깃2", "A10N3", "ubist", "sales", 90)
    competitor = _row("competitor", "경쟁", "A10N3", "ubist", "sales", 80)
    brand_rows = [target_one, target_two]
    market_rows = [target_one, target_two, competitor]
    market_calls: list[tuple[int, str, int]] = []
    entry_calls: list[str] = []

    monkeypatch.setattr(general_builder, "fetch_rows_for_groups", lambda _conn, _keys: brand_rows)
    monkeypatch.setattr(general_builder, "fetch_market_rows_for_atc4s", lambda _conn, _atc4s: market_rows)
    monkeypatch.setattr(general_builder, "load_brand_factor_map", lambda _conn, _brands: {})

    def fake_market(rows: list[dict[str, Any]], source: str, steps: int) -> dict[str, Any]:
        market_calls.append((len(rows), source, steps))
        return {"history_periods": ["2025-01", "2025-02"], "history_values": [1.0, 2.0], "forecast_values": [3.0, 4.0]}

    def fake_entry(
        brand_row: dict[str, Any],
        *,
        target_brand: str,
        source: str,
        measure: str,
        forecast_steps_count: int,
    ) -> dict[str, Any]:
        entry_calls.append(str(brand_row["brand_key"]))
        return {
            "brand": brand_row["brand_name"],
            "is_target": brand_row["brand_name"] == target_brand,
            "is_jw": bool(brand_row["is_jw"]),
            "rank": 1,
            "history_values": [1.0, 2.0],
            "forecast_values": [3.0, 4.0],
            "forecast_model": {"name": "Mean", "selection_policy": "data_size_dispatch_v1"},
        }

    monkeypatch.setattr(general_builder, "build_market_forecast", fake_market)
    monkeypatch.setattr(general_builder, "build_forecast_brand_entry", fake_entry)
    monkeypatch.setattr(general_builder, "build_simulation_combo", lambda **_kwargs: {"ok": True})

    # When: both brand+ATC4 cache rows are built in one batch.
    rows = general_builder.build_batch_rows(
        None,
        [("target-1", "A10N3"), ("target-2", "A10N3")],
        workers=1,
        verbose=False,
    )

    # Then: market and brand forecast work is computed once per reusable input,
    # while each target row still receives its own target marker in the payload.
    assert len(rows) == 2
    assert market_calls == [(3, "UBIST", 120)]
    assert sorted(entry_calls) == ["competitor", "target-1", "target-2"]
    payloads = [json.loads(row.response_json) for row in rows]
    target_flags = {
        payload["brand_key"]: [
            (entry["brand"], entry["is_target"])
            for entry in payload["data"]["forecast"]["by_combo"]["UBIST.sales"]["brands"]
        ]
        for payload in payloads
    }
    assert target_flags["target-1"][0] == ("타깃1", True)
    assert target_flags["target-2"][0] == ("타깃2", True)


def test_market_forecasts_use_persistent_cache_for_fresh_combos(monkeypatch) -> None:
    # Given: one ATC4/source/measure combo is already present in the market
    # forecast cache and another combo is missing.
    cached_key = ("A10N3", "ubist", "sales")
    missing_key = ("A10N3", "iqvia_nsa", "sales")
    computed_keys: list[tuple[str, str, str]] = []
    upserted: dict[tuple[str, str, str], dict[str, Any]] = {}

    monkeypatch.setattr(general_builder, "ensure_market_forecast_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        general_builder,
        "load_market_forecast_cache",
        lambda *_args, **_kwargs: {cached_key: {"cached": True}},
    )

    def fake_upsert(_conn: object, forecasts: dict[tuple[str, str, str], dict[str, Any]], *_args: object, **_kwargs: object) -> None:
        upserted.update(forecasts)

    def fake_market(rows: list[dict[str, Any]], source: str, _steps: int) -> dict[str, Any]:
        computed_keys.append((str(rows[0]["atc4_code"]), source, str(rows[0]["measure"])))
        return {"computed": source}

    monkeypatch.setattr(general_builder, "upsert_market_forecast_cache", fake_upsert)
    monkeypatch.setattr(general_builder, "build_market_forecast", fake_market)

    # When: both combos are requested with a persistent cache connection.
    forecasts = general_builder.build_market_forecasts_by_combo(
        {
            cached_key: [_row("target", "타깃", "A10N3", "ubist", "sales", 100)],
            missing_key: [_row("target", "타깃", "A10N3", "iqvia_nsa", "sales", 100)],
        },
        workers=1,
        conn=object(),
    )

    # Then: only the missing combo is computed and persisted.
    assert forecasts[cached_key] == {"cached": True}
    assert forecasts[missing_key] == {"computed": "IQVIA"}
    assert computed_keys == [("A10N3", "IQVIA", "sales")]
    assert sorted(upserted) == [missing_key]


def test_priority_group_keys_rank_by_recent_sales_descending() -> None:
    rows = [
        _row("low", "낮음", "A01", "ubist", "sales", 10),
        _row("high", "높음", "A01", "ubist", "sales", 100),
        _row("high", "높음", "A01", "iqvia_nsa", "sales", 50),
        _row("middle", "중간", "B02", "ubist", "sales", 70),
    ]

    selected = general_builder.priority_group_keys_from_rows(rows, limit_groups=2)

    assert selected == [("high", "A01"), ("middle", "B02")]


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
