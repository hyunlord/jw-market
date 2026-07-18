"""Forecast and simulation payload assembly for one native market scope."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder
from pipeline.scripts.etl.build_cache_deep_analysis import (
    ALL_COMBOS,
    FORECAST_DISCLOSURE,
    FORECAST_METHOD,
    top6_rows,
)
from pipeline.scripts.etl.cache_build_common import api_source, dump_payload
from pipeline.scripts.etl.general_forecast_payload import optimize_and_mark_payload, validate_payload_contract
from pipeline.scripts.etl.ops_forecast_scope import BlockRow, HORIZON_YEARS, HorizonRow, Scope, Unit
from pipeline.scripts.forecast.forecast_runner import (
    build_forecast_brand_entry,
    build_market_forecast,
    build_simulation_combo,
    forecast_steps,
)


def _steps(source: str) -> int:
    return forecast_steps(api_source(source)) * HORIZON_YEARS // 10


def _market_products(scope: Scope, rows: list[dict[str, Any]], workers: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    rows_by_measure: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_measure[str(row["measure"])].append(row)
    forecasts: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(build_market_forecast, measure_rows, api_source(scope.source), _steps(scope.source)): measure
            for measure, measure_rows in sorted(rows_by_measure.items())
        }
        for future in as_completed(futures):
            forecasts[futures[future]] = future.result()
    return rows_by_measure, forecasts


def _payload(unit: Unit, unit_rows: list[dict[str, Any]], rows_by_measure: dict[str, list[dict[str, Any]]], market_forecasts: dict[str, dict[str, Any]], generated_at_iso: str) -> dict[str, Any]:
    base = general_builder.choose_base(unit_rows)
    brand = str(base.get("brand_name") or unit.brand_key)
    source_api = api_source(unit.scope.source)
    rows_by_combo = {f"{api_source(row['source'])}.{row['measure']}": row for row in unit_rows}
    forecasts: dict[str, Any] = {}
    simulations: dict[str, Any] = {}
    for combo_source, measure in ALL_COMBOS:
        if combo_source != source_api:
            continue
        combo = f"{combo_source}.{measure}"
        row = rows_by_combo.get(combo)
        if row is None:
            continue
        market_forecast = market_forecasts[measure]
        selected = top6_rows(rows_by_measure[measure], brand) or [row]
        selected_entries = [
            build_forecast_brand_entry(item, target_brand="", source=combo_source, measure=measure, forecast_steps_count=_steps(unit.scope.source))
            for item in selected
        ]
        combo_data = general_builder.combo_payload(
            row,
            market_forecast=market_forecast,
            selected_entries=selected_entries,
            target_brand=brand,
            source=combo_source,
            forecast_steps_count=_steps(unit.scope.source),
        )
        combo_data.pop("_market_forecast", None)
        forecasts[combo] = combo_data
        simulations[combo] = build_simulation_combo(
            combo=combo,
            source=combo_source,
            measure=measure,
            unit_label=combo_data.get("unit_label"),
            forecast_combo=combo_data,
            market_forecast=market_forecast,
            cut_b_events=[],
        )
    payload = optimize_and_mark_payload(
        general_builder.json_safe(
            {
                "brand": brand,
                "brand_name": brand,
                "brand_key": unit.brand_key,
                "market_id": unit.scope.market_id,
                "view_kind": unit.scope.view_kind,
                "available_combos": sorted(forecasts),
                "data": {
                    "forecast": {
                        "method": FORECAST_METHOD,
                        "disclaimer": FORECAST_DISCLOSURE,
                        "is_statistical_model": True,
                        "backtest_available": True,
                        "event_regressor_enabled": False,
                        "phase29_poc": None,
                        "by_combo": forecasts,
                    },
                    "simulation": {"by_combo": simulations},
                    "events": [],
                },
            }
        )
    )
    validate_payload_contract(payload)
    payload["generated_at"] = generated_at_iso
    payload["timestamp_source"] = "producer"
    return payload


def build_scope_rows(scope: Scope, units: list[Unit], native_rows: list[dict[str, Any]], *, workers: int, source_epoch: str, generated_at: datetime) -> tuple[list[BlockRow], list[HorizonRow]]:
    rows_by_measure, market_forecasts = _market_products(scope, native_rows, workers)
    rows_by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in native_rows:
        rows_by_brand[str(row["brand_key"])].append(row)
    computed_values = [row["computed_at"] for row in native_rows if row.get("computed_at") is not None]
    computed_at = max(computed_values) if computed_values else None
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    blocks: list[BlockRow] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_payload, unit, rows_by_brand[unit.brand_key], rows_by_measure, market_forecasts, generated_at_iso): unit
            for unit in units
        }
        for future in as_completed(futures):
            unit = futures[future]
            payload = future.result()
            forecast_section = dict(payload["data"]["forecast"])
            forecast_section["generated_at"] = generated_at_iso
            forecast_section["timestamp_source"] = "producer"
            availability = payload["simulation_available"]
            simulation_available = any(bool(value) for value in availability.values())
            blocks.append(
                BlockRow(
                    unit.brand_key,
                    scope.source,
                    scope.market_id,
                    scope.view_kind,
                    dump_payload(forecast_section),
                    dump_payload(payload["data"]["simulation"]) if simulation_available else None,
                    str(payload["generation_status"]),
                    dump_payload(payload["no_history_fallback"]),
                    int(simulation_available),
                    source_epoch,
                    computed_at,
                    generated_at,
                )
            )
    horizons = [
        HorizonRow(
            scope.market_id,
            scope.source,
            measure,
            scope.view_kind,
            dump_payload({**forecast, "generated_at": generated_at_iso, "timestamp_source": "producer"}),
            len(rows_by_measure[measure]),
            source_epoch,
            max((row["computed_at"] for row in rows_by_measure[measure] if row.get("computed_at") is not None), default=None),
            generated_at,
        )
        for measure, forecast in sorted(market_forecasts.items())
    ]
    return sorted(blocks, key=lambda row: row.brand_key), horizons
