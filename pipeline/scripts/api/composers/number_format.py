from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Any


DISPLAY_KEY_SUFFIXES = ("_display", "_formatted")
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
    value_type = type(value)
    if value_type is float:
        if math.isnan(value) or math.isinf(value):
            return None
        return _truncate_float(value)
    if value is None or value_type is bool or value_type is int or value_type is str:
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return _truncate_float(value)
    if isinstance(value, Decimal):
        return float(value.quantize(_FOUR_DECIMAL_PLACES, rounding=ROUND_DOWN))
    return value


def deep_format_numbers(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: deep_format_numbers(item)
            for key, item in value.items()
            if not str(key).endswith(DISPLAY_KEY_SUFFIXES)
        }
    if isinstance(value, list):
        return [deep_format_numbers(item) for item in value]
    return format_number(value)
