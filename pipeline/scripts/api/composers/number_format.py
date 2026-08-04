from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any


DISPLAY_KEY_SUFFIXES = ("_display", "_formatted")
HHI_VALUE_KEYS = frozenset({"hhi", "hhi_recent", "hhi_values"})
_FOUR_DECIMAL_PLACES = Decimal("0.0001")
_FLOAT_FAST_PATH_LIMIT = 100_000_000.0
_SCALED_BOUNDARY_EPSILON = 0.00025


def _truncate_float(value: float) -> float:
    if abs(value) < _FLOAT_FAST_PATH_LIMIT:
        scaled = value * 10_000.0
        integer = math.trunc(scaled)
        fraction = abs(scaled - integer)
        if _SCALED_BOUNDARY_EPSILON <= fraction <= 1.0 - _SCALED_BOUNDARY_EPSILON:
            if integer == 0:
                return math.copysign(0.0, value)
            return integer / 10_000.0

    text = str(value)
    if "e" not in text and "E" not in text:
        decimal_point = text.find(".")
        if decimal_point < 0 or len(text) <= decimal_point + 5:
            return value
        return float(text[: decimal_point + 5])
    return float(Decimal(text).quantize(_FOUR_DECIMAL_PLACES, rounding=ROUND_DOWN))


def format_number(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return _truncate_float(value)
    if isinstance(value, Decimal):
        return float(value.quantize(_FOUR_DECIMAL_PLACES, rounding=ROUND_DOWN))
    return value


def format_number_for_key(key: str | None, value: Any) -> Any:
    if key not in HHI_VALUE_KEYS:
        return format_number(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(
            Decimal(str(value)).quantize(
                _FOUR_DECIMAL_PLACES,
                rounding=ROUND_HALF_UP,
            )
        )
    if isinstance(value, Decimal):
        return float(value.quantize(_FOUR_DECIMAL_PLACES, rounding=ROUND_HALF_UP))
    return value


def deep_format_numbers(value: Any, *, _field_name: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: deep_format_numbers(item, _field_name=str(key))
            for key, item in value.items()
            if not str(key).endswith(DISPLAY_KEY_SUFFIXES)
        }
    if isinstance(value, list):
        return [deep_format_numbers(item, _field_name=_field_name) for item in value]
    return format_number_for_key(_field_name, value)
