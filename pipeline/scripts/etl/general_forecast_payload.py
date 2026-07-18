"""Optimize and validate general forecast/simulation cache payloads."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Final

from pipeline.scripts.etl import build_cache_deep_analysis_general as builder
from pipeline.scripts.etl.cache_build_common import dump_payload


FORECAST_HORIZON_MONTHS: Final[int] = 60
FORECAST_HORIZON_QUARTERS: Final[int] = 20
REQUIRED_SCENARIOS: Final[tuple[str, str, str]] = ("base", "upper", "lower")
ARRAY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "forecast_periods",
        "forecast_values",
        "forecast_ms_pct",
        "values",
        "ci_upper_95",
        "ci_lower_95",
        "upper_95_natural",
        "lower_95_natural",
        "upper_horizon_adaptive",
        "lower_horizon_adaptive",
    }
)


@dataclass(frozen=True, slots=True)
class ContractGateError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _horizon(period_unit: str | None) -> int | None:
    if period_unit == "월":
        return FORECAST_HORIZON_MONTHS
    if period_unit == "분기":
        return FORECAST_HORIZON_QUARTERS
    return None


def _slice_arrays(value: Any, horizon: int) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in ARRAY_KEYS and isinstance(item, list):
                result[key] = item[:horizon]
            else:
                result[key] = _slice_arrays(item, horizon)
        return result
    if isinstance(value, list):
        return [_slice_arrays(item, horizon) for item in value]
    return value


def _remove_duplicate_interval_aliases(value: Any) -> None:
    if isinstance(value, dict):
        pairs = (
            ("upper_horizon_adaptive", "upper_95_natural"),
            ("lower_horizon_adaptive", "lower_95_natural"),
        )
        for alias, canonical in pairs:
            if alias in value and canonical in value and value[alias] == value[canonical]:
                del value[alias]
        for item in value.values():
            _remove_duplicate_interval_aliases(item)
    elif isinstance(value, list):
        for item in value:
            _remove_duplicate_interval_aliases(item)


def _target_forecast_brand(combo: dict[str, Any]) -> dict[str, Any] | None:
    brands = combo.get("brands")
    if not isinstance(brands, list):
        return None
    for brand in brands:
        if isinstance(brand, dict) and brand.get("is_target"):
            return brand
    return next((brand for brand in brands if isinstance(brand, dict)), None)


def _history_marker(history_values: list[Any]) -> dict[str, Any]:
    if not history_values:
        return {"applied": True, "reason": "no_history"}
    numeric = [float(value) for value in history_values if isinstance(value, (int, float))]
    if numeric and not any(value != 0.0 for value in numeric):
        return {"applied": False, "reason": "actual_zero_history"}
    return {"applied": False, "reason": "history_present"}


def _markers(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    forecast = data.get("forecast") if isinstance(data.get("forecast"), dict) else {}
    forecast_by_combo = forecast.get("by_combo") if isinstance(forecast.get("by_combo"), dict) else {}
    simulation = data.get("simulation") if isinstance(data.get("simulation"), dict) else {}
    simulation_by_combo = simulation.get("by_combo") if isinstance(simulation.get("by_combo"), dict) else {}
    history_length: dict[str, int] = {}
    no_history_fallback: dict[str, dict[str, Any]] = {}
    forecast_has_nonzero: dict[str, bool] = {}
    simulation_available: dict[str, bool] = {}
    for combo_name, combo_value in forecast_by_combo.items():
        combo = combo_value if isinstance(combo_value, dict) else {}
        target = _target_forecast_brand(combo) or {}
        history = target.get("history_values") if isinstance(target.get("history_values"), list) else []
        values = target.get("forecast_values") if isinstance(target.get("forecast_values"), list) else []
        history_length[str(combo_name)] = len(history)
        no_history_fallback[str(combo_name)] = _history_marker(history)
        forecast_has_nonzero[str(combo_name)] = any(
            isinstance(value, (int, float)) and float(value) != 0.0 for value in values
        )
        simulation_combo = simulation_by_combo.get(combo_name)
        by_brand = simulation_combo.get("by_brand") if isinstance(simulation_combo, dict) else None
        simulation_available[str(combo_name)] = bool(isinstance(by_brand, dict) and by_brand)
    return {
        "generation_status": "generated",
        "history_length": history_length,
        "no_history_fallback": no_history_fallback,
        "forecast_has_nonzero": forecast_has_nonzero,
        "simulation_available": simulation_available,
    }


def optimize_and_mark_payload(payload: dict[str, Any]) -> dict[str, Any]:
    optimized = json.loads(json.dumps(payload, ensure_ascii=False))
    data = optimized.get("data") if isinstance(optimized.get("data"), dict) else {}
    for section_name in ("forecast", "simulation"):
        section = data.get(section_name)
        by_combo = section.get("by_combo") if isinstance(section, dict) else None
        if not isinstance(by_combo, dict):
            continue
        for combo_name, combo_value in list(by_combo.items()):
            if not isinstance(combo_value, dict):
                continue
            horizon = _horizon(str(combo_value.get("period_unit") or ""))
            if horizon is not None:
                by_combo[combo_name] = _slice_arrays(combo_value, horizon)
    _remove_duplicate_interval_aliases(optimized)
    optimized.update(_markers(optimized))
    optimized["payload_optimization"] = {
        "horizon": "5y_api_contract",
        "encoding": "compact_json",
        "deduplication": "identical_interval_aliases",
    }
    return optimized


def validate_payload_contract(payload: dict[str, Any]) -> None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    forecast = data.get("forecast") if isinstance(data.get("forecast"), dict) else {}
    forecast_by_combo = forecast.get("by_combo") if isinstance(forecast.get("by_combo"), dict) else {}
    simulation = data.get("simulation") if isinstance(data.get("simulation"), dict) else {}
    simulation_by_combo = simulation.get("by_combo") if isinstance(simulation.get("by_combo"), dict) else {}
    if not forecast_by_combo:
        raise ContractGateError("forecast_missing:all")
    for combo_name, combo_value in forecast_by_combo.items():
        combo = combo_value if isinstance(combo_value, dict) else {}
        target = _target_forecast_brand(combo)
        values = target.get("forecast_values") if isinstance(target, dict) else None
        if not isinstance(values, list) or not values:
            raise ContractGateError(f"forecast_missing:{combo_name}")
        simulation_combo = simulation_by_combo.get(combo_name)
        by_brand = simulation_combo.get("by_brand") if isinstance(simulation_combo, dict) else None
        if not isinstance(by_brand, dict) or not by_brand:
            raise ContractGateError(f"simulation_missing:{combo_name}")
        target_name = str(target.get("brand") or "")
        simulation_brand = by_brand.get(target_name)
        if not isinstance(simulation_brand, dict):
            simulation_brand = next((item for item in by_brand.values() if isinstance(item, dict)), None)
        scenarios = simulation_brand.get("scenarios") if isinstance(simulation_brand, dict) else None
        if not isinstance(scenarios, dict):
            raise ContractGateError(f"scenarios_missing:{combo_name}")
        for scenario in REQUIRED_SCENARIOS:
            scenario_payload = scenarios.get(scenario)
            scenario_values = scenario_payload.get("values") if isinstance(scenario_payload, dict) else None
            if not isinstance(scenario_values, list) or not scenario_values:
                raise ContractGateError(f"scenario_missing:{scenario}:{combo_name}")


def optimize_validate_and_serialize(payload: dict[str, Any]) -> str:
    optimized = optimize_and_mark_payload(payload)
    validate_payload_contract(optimized)
    return dump_payload(optimized)


def transform_cache_row(row: builder.GeneralCacheRow) -> builder.GeneralCacheRow:
    response_json = optimize_validate_and_serialize(json.loads(row.response_json))
    return replace(row, response_json=response_json, payload_size=len(response_json.encode("utf-8")))
