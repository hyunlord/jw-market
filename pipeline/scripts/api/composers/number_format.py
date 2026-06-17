from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Any


DISPLAY_KEY_SUFFIXES = ("_display", "_formatted")


def format_number(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
    if isinstance(value, Decimal):
        return float(value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
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
