from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DeltaOperands:
    from_value: Any
    to_value: Any
    delta_value: Any
    decimals: int = 2


@dataclass(frozen=True, slots=True)
class CagrOperands:
    start_period: Any = None
    start_value: Any = None
    end_period: Any = None
    end_value: Any = None
    year_count: Any = None
    formula: str = ""
    decimals: int = 2


def can_surface_derived_value(
    value: Any,
    *,
    required_period: str | None = None,
    delta_operands: DeltaOperands | None = None,
    cagr_operands: CagrOperands | None = None,
) -> bool:
    """Return whether a derived/external value has the context required for display."""
    if value in (None, "", "-"):
        return False
    if required_period is not None and not str(required_period).strip():
        return False
    if delta_operands is not None:
        return _delta_operands_are_reproducible(delta_operands)
    if cagr_operands is not None:
        return _cagr_operands_are_reproducible(value, cagr_operands)
    return True


def _delta_operands_are_reproducible(operands: DeltaOperands) -> bool:
    from_value = _rounded_float(operands.from_value, operands.decimals)
    to_value = _rounded_float(operands.to_value, operands.decimals)
    delta_value = _rounded_float(operands.delta_value, operands.decimals)
    if from_value is None or to_value is None or delta_value is None:
        return False
    calculated = _rounded_float(to_value - from_value, operands.decimals)
    return calculated == delta_value


def _cagr_operands_are_reproducible(value: Any, operands: CagrOperands) -> bool:
    if not str(operands.start_period or "").strip():
        return False
    if not str(operands.end_period or "").strip():
        return False
    if not str(operands.formula or "").strip():
        return False
    start_value = _positive_float(operands.start_value)
    end_value = _positive_float(operands.end_value)
    years = _positive_float(operands.year_count)
    cagr_value = _rounded_float(value, operands.decimals)
    if start_value is None or end_value is None or years is None or cagr_value is None:
        return False
    calculated = _rounded_float(((end_value / start_value) ** (1 / years) - 1) * 100, operands.decimals)
    return calculated == cagr_value


def _rounded_float(value: Any, decimals: int) -> float | None:
    if not isinstance(value, int | float):
        return None
    return float(f"{float(value):.{decimals}f}")


def _positive_float(value: Any) -> float | None:
    if not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number > 0 else None


def cagr_operands_from_data(data: dict[str, Any], key: str) -> CagrOperands:
    prefix = key.removesuffix("_pct")
    return CagrOperands(
        start_period=data.get(f"{prefix}_start_period") or data.get("cagr_start_period"),
        start_value=data.get(f"{prefix}_start_value") or data.get("cagr_start_value"),
        end_period=data.get(f"{prefix}_end_period") or data.get("cagr_end_period"),
        end_value=data.get(f"{prefix}_end_value") or data.get("cagr_end_value"),
        year_count=data.get(f"{prefix}_year_count") or data.get("cagr_year_count"),
        formula=str(data.get(f"{prefix}_formula") or data.get("cagr_formula") or ""),
    )


def request_value(data: dict[str, Any], key: str) -> Any:
    request = data.get("request")
    if isinstance(request, dict):
        return request.get(key)
    return None


def surface_year(data: dict[str, Any], item: dict[str, Any] | None = None) -> str:
    """Return the period year that makes a HIRA patient-count value displayable."""
    raw = request_value(data, "year") or data.get("year")
    if raw is None and item is not None:
        raw = item.get("year")
    return str(raw).strip() if raw not in (None, "") else ""
